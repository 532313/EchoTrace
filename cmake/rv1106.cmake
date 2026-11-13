set(ECHO_MEDIA_SOURCE_ROOT "" CACHE PATH "Existing security project's RV1106 directory")
if(NOT EXISTS "${ECHO_MEDIA_SOURCE_ROOT}/include/rk_mpi_sys.h")
  message(FATAL_ERROR "ECHO_MEDIA_SOURCE_ROOT must point to the existing RV1106 source snapshot")
endif()
if(NOT TARGET echo_recording)
  message(FATAL_ERROR "Prepare the recording modules with scripts/prepare_sources.py")
endif()
find_path(ALSA_INCLUDE_DIR alsa/asoundlib.h REQUIRED)
find_library(ALSA_LIBRARY asound REQUIRED)
target_sources(echotrace-edge PRIVATE native/src/capture_rv1106.cpp)
target_compile_definitions(echotrace-edge PRIVATE ECHO_RV1106 ISP_HW_V30 RKPLATFORM UAPI2)
target_include_directories(echotrace-edge PRIVATE ${ALSA_INCLUDE_DIR} ${ECHO_MEDIA_SOURCE_ROOT}/include
  ${ECHO_MEDIA_SOURCE_ROOT}/include/rkaiq ${ECHO_MEDIA_SOURCE_ROOT}/include/rkaiq/uAPI2
  ${ECHO_MEDIA_SOURCE_ROOT}/include/rkaiq/common ${ECHO_MEDIA_SOURCE_ROOT}/include/rkaiq/xcore
  ${ECHO_MEDIA_SOURCE_ROOT}/include/rkaiq/iq_parser ${ECHO_MEDIA_SOURCE_ROOT}/include/rkaiq/iq_parser_v2
  ${ECHO_MEDIA_SOURCE_ROOT}/include/rkaiq/algos)
set(ECHO_MEDIA_LIBRARY_DIR "${ECHO_MEDIA_SOURCE_ROOT}/lib/uclibc" CACHE PATH "RV1106 vendor media libraries")
if(NOT EXISTS "${ECHO_MEDIA_LIBRARY_DIR}")
  set(ECHO_MEDIA_LIBRARY_DIR "${ECHO_MEDIA_SOURCE_ROOT}/lib")
endif()
target_link_directories(echotrace-edge PRIVATE ${ECHO_MEDIA_LIBRARY_DIR})
target_link_options(echotrace-edge PRIVATE "-Wl,-rpath-link,${ECHO_MEDIA_LIBRARY_DIR}" "-Wl,-rpath-link,${CMAKE_SYSROOT}/usr/lib")
target_link_libraries(echotrace-edge PRIVATE echo_recording ${ALSA_LIBRARY} rockit rockchip_mpp rkaiq sample_comm rtsp)
set_target_properties(echotrace-edge PROPERTIES INSTALL_RPATH "$ORIGIN/../lib")
