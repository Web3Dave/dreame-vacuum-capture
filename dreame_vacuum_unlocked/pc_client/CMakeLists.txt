cmake_minimum_required(VERSION 3.15.0)
project(p2p_sample C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_FLAGS "-Wall -Wextra -Wno-unused-parameter -O2")

include_directories(${CMAKE_CURRENT_SOURCE_DIR}/src)

add_executable(p2p_sample src/p2p_sample.c src/p2p_api.c)

target_link_directories(p2p_sample PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/lib/linux/x86/Release)
target_link_libraries(p2p_sample
    app_interface enet-core curl z
    event_extra event_core event_mbedtls event_pthreads
    tinyxml2 minizip mbedtls mbedcrypto mbedx509
    pthread stdc++ m
)
