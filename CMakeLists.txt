cmake_minimum_required(VERSION 3.16)
project(coz-mcp CXX C)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
find_package(Threads REQUIRED)

# Latch hooks need the coz-mcp additions to coz.h.
option(COZ_MCP_LATCH_HOOKS "Build demo with coz-mcp latch hooks" OFF)

enable_testing()
add_subdirectory(coz)
add_subdirectory(demo)
