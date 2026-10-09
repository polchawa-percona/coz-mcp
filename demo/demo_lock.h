// Lock types used by the demo.
//
// PthreadLock: std::mutex, i.e. pthread_mutex_* which Coz interposes.
// SpinEventLock: test-and-test-and-set spin lock that falls back to a
// condition variable, the same shape as InnoDB's TTASEventMutex. It is inline
// code, so Coz cannot interpose it. With Hooked = true it calls the coz-mcp
// latch hooks (wait begin / acquired / release), like the Percona Server
// WITH_COZ build does for InnoDB latches.

#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <mutex>

#ifdef DEMO_HAVE_COZ_LATCH
#include "coz.h"
#endif

namespace demo {

class PthreadLock {
 public:
  void lock(const char *, int) { m_mutex.lock(); }
  void unlock() { m_mutex.unlock(); }

 private:
  std::mutex m_mutex;
};

template <bool Hooked>
class SpinEventLock {
 public:
  void lock(const char *file, int line) {
#ifdef DEMO_HAVE_COZ_LATCH
    if (Hooked)
      COZ_LATCH_WAIT_BEGIN(this, file, line, "demo_lock", COZ_LATCH_MUTEX);
#else
    (void)file;
    (void)line;
#endif
    for (;;) {
      for (int i = 0; i < kSpinRounds; ++i) {
        if (!m_locked.load(std::memory_order_relaxed) &&
            !m_locked.exchange(true, std::memory_order_acquire)) {
          acquired();
          return;
        }
        __builtin_ia32_pause();
      }
      std::unique_lock<std::mutex> guard(m_event_mutex);
      // seq_cst pairs with unlock(): either we see the lock free, or the
      // unlocker sees our waiter count and signals.
      m_waiters.fetch_add(1);
      if (m_locked.load())
        m_event.wait_for(guard, std::chrono::milliseconds(1));
      m_waiters.fetch_sub(1);
    }
  }

  void unlock() {
#ifdef DEMO_HAVE_COZ_LATCH
    if (Hooked) COZ_LATCH_RELEASE(this);
#endif
    m_locked.store(false);
    if (m_waiters.load() > 0) {
      std::lock_guard<std::mutex> guard(m_event_mutex);
      m_event.notify_one();
    }
  }

 private:
  void acquired() {
#ifdef DEMO_HAVE_COZ_LATCH
    if (Hooked) COZ_LATCH_ACQUIRED(this);
#endif
  }

  static constexpr int kSpinRounds = 30;
  std::atomic<bool> m_locked{false};
  std::atomic<int> m_waiters{0};
  std::mutex m_event_mutex;
  std::condition_variable m_event;
};

}  // namespace demo
