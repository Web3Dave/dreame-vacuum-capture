cmake_minimum_required(VERSION 3.15.0)
project(dreame_p2p C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_FLAGS "-Wall -Wextra -Wno-unused-parameter -O2")

include_directories(${CMAKE_CURRENT_SOURCE_DIR}/src)

set(P2P_LIBS
    app_interface enet-core curl z
    event_extra event_core event_mbedtls event_pthreads
    tinyxml2 minizip mbedtls mbedcrypto mbedx509
    pthread stdc++ m
)

add_executable(p2p_sample src/p2p_sample.c src/p2p_api.c)
target_link_directories(p2p_sample PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/lib/linux/x86/Release)
target_link_libraries(p2p_sample ${P2P_LIBS})

# Talk-back: streams audio to the vacuum's speaker over the XP2P voice channel.
add_executable(p2p_speak src/p2p_speak.c src/p2p_api.c)
target_link_directories(p2p_speak PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/lib/linux/x86/Release)
target_link_libraries(p2p_speak ${P2P_LIBS})

