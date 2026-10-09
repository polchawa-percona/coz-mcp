// Demo workload with a known bottleneck for validating coz-mcp.
//
// Each worker thread runs "transactions" in a loop:
//   1. parallel work (line marked COZ-LINE: P), no lock held,
//   2. serial work (line marked COZ-LINE: S) under a single global lock.
// With enough threads the serial section saturates and becomes the bottleneck.
//
// Every second the main thread prints a report line in sysbench 1.0 format:
//   [ 1s ] thds: 4 tps: 1234.00 lat (ms,95%): 0.00
// so the same parser handles the demo and real sysbench.

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "demo_lock.h"

namespace {

struct Options {
  int threads = 4;
  int time_s = 10;
  int parallel_work = 20000;  // loop iterations outside the lock
  int serial_work = 5000;     // loop iterations inside the lock
  int serial_work_pct = 100;  // real speedup/slowdown of the serial section
  int parallel_work_pct = 100;
  std::string lock = "pthread";
};

std::atomic<bool> g_stop{false};
std::atomic<uint64_t> g_txns{0};

// Opaque sink so the compiler cannot drop the work loops.
std::atomic<uint64_t> g_sink{0};

// Each work loop is on a single line so that all of its samples map to the
// marked line.
constexpr uint64_t kMulP = 6364136223846793005ULL;
constexpr uint64_t kAddP = 1442695040888963407ULL;
constexpr uint64_t kMulS = 0x9E3779B97F4A7C15ULL;

__attribute__((noinline)) uint64_t parallel_work(uint64_t seed, int n) {
  uint64_t x = seed;
  for (int i = 0; i < n; ++i) x = x * kMulP + kAddP;  // COZ-LINE: P
  return x;
}

__attribute__((noinline)) uint64_t serial_work(uint64_t seed, int n) {
  uint64_t x = seed;
  for (int i = 0; i < n; ++i) x = (x ^ (x >> 13)) * kMulS + i;  // COZ-LINE: S
  return x;
}

template <typename Lock>
void worker(Lock &lock, const Options &opt, int id) {
  const int p = opt.parallel_work * opt.parallel_work_pct / 100;
  const int s = opt.serial_work * opt.serial_work_pct / 100;
  uint64_t x = static_cast<uint64_t>(id) + 1;
  uint64_t shared_state = 0;
  while (!g_stop.load(std::memory_order_relaxed)) {
    x = parallel_work(x, p);
    lock.lock(__FILE__, __LINE__);
    shared_state = serial_work(shared_state ^ x, s);
    lock.unlock();
    g_txns.fetch_add(1, std::memory_order_relaxed);
  }
  g_sink.fetch_add(x ^ shared_state, std::memory_order_relaxed);
}

template <typename Lock>
void run(const Options &opt) {
  Lock lock;
  std::vector<std::thread> threads;
  threads.reserve(opt.threads);
  for (int i = 0; i < opt.threads; ++i) {
    threads.emplace_back([&lock, &opt, i] { worker(lock, opt, i); });
  }

  auto start = std::chrono::steady_clock::now();
  uint64_t last = 0;
  for (int sec = 1; sec <= opt.time_s; ++sec) {
    std::this_thread::sleep_until(start + std::chrono::seconds(sec));
    uint64_t now = g_txns.load(std::memory_order_relaxed);
    std::printf("[ %ds ] thds: %d tps: %.2f lat (ms,95%%): 0.00\n", sec,
                opt.threads, static_cast<double>(now - last));
    std::fflush(stdout);
    last = now;
  }
  g_stop.store(true);
  for (auto &t : threads) t.join();
  std::printf("total transactions: %llu\n",
              static_cast<unsigned long long>(g_txns.load()));
}

void usage() {
  std::fprintf(stderr,
               "usage: demo [--threads=N] [--time=S] [--parallel-work=N]\n"
               "            [--serial-work=N] [--serial-work-pct=P]\n"
               "            [--parallel-work-pct=P] "
               "[--lock=pthread|spin|spin-hooked]\n");
}

bool parse(int argc, char **argv, Options &opt) {
  for (int i = 1; i < argc; ++i) {
    const char *a = argv[i];
    auto val = [a](const char *name) -> const char * {
      size_t n = std::strlen(name);
      return std::strncmp(a, name, n) == 0 && a[n] == '=' ? a + n + 1 : nullptr;
    };
    const char *v;
    if ((v = val("--threads")))
      opt.threads = std::atoi(v);
    else if ((v = val("--time")))
      opt.time_s = std::atoi(v);
    else if ((v = val("--parallel-work")))
      opt.parallel_work = std::atoi(v);
    else if ((v = val("--serial-work")))
      opt.serial_work = std::atoi(v);
    else if ((v = val("--serial-work-pct")))
      opt.serial_work_pct = std::atoi(v);
    else if ((v = val("--parallel-work-pct")))
      opt.parallel_work_pct = std::atoi(v);
    else if ((v = val("--lock")))
      opt.lock = v;
    else
      return false;
  }
  return opt.threads > 0 && opt.time_s > 0;
}

}  // namespace

int main(int argc, char **argv) {
  Options opt;
  if (!parse(argc, argv, opt)) {
    usage();
    return 2;
  }
  if (opt.lock == "pthread") {
    run<demo::PthreadLock>(opt);
  } else if (opt.lock == "spin") {
    run<demo::SpinEventLock<false>>(opt);
  } else if (opt.lock == "spin-hooked") {
    run<demo::SpinEventLock<true>>(opt);
  } else {
    usage();
    return 2;
  }
  return 0;
}
