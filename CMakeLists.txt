cmake_minimum_required(VERSION 3.16)
project(coz-mcp CXX C)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
find_package(Threads REQUIRED)


enable_testing()
add_subdirectory(coz)
add_subdirectory(demo)
