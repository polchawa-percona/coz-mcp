add_executable(demo demo.cc)
target_compile_features(demo PRIVATE cxx_std_17)
target_compile_options(demo PRIVATE -g -fno-omit-frame-pointer)
target_link_libraries(demo PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
# Latch hooks (--lock=spin-hooked) come from the Coz fork's coz.h; they are
# no-ops when the demo does not run under libcoz.
target_compile_definitions(demo PRIVATE DEMO_HAVE_COZ_LATCH)
target_include_directories(demo PRIVATE ${PROJECT_SOURCE_DIR}/coz/include)
