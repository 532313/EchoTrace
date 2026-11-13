# Python package metadata is the single source of the product version.
file(STRINGS "${CMAKE_CURRENT_LIST_DIR}/../src/echotrace/__init__.py"
  _echo_version_line REGEX "^__version__ = ")
string(REGEX REPLACE "^__version__ = \"([^\"]+)\"$" "\\1"
  ECHO_VERSION "${_echo_version_line}")
if(NOT ECHO_VERSION MATCHES "^[0-9]+\\.[0-9]+\\.[0-9]+((a|b|rc)[0-9]+|\\.dev[0-9]+)?$")
  message(FATAL_ERROR "Invalid EchoTrace product version: ${ECHO_VERSION}")
endif()
string(REGEX MATCH "^[0-9]+\\.[0-9]+\\.[0-9]+" ECHO_NUMERIC_VERSION "${ECHO_VERSION}")
set(ECHO_SOURCE_REVISION "unknown" CACHE STRING "Source commit for exported/SDK builds")
if(ECHO_SOURCE_REVISION STREQUAL "unknown")
  find_package(Git QUIET)
  if(GIT_FOUND)
    execute_process(COMMAND "${GIT_EXECUTABLE}" rev-parse --short=12 HEAD
      WORKING_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/.."
      OUTPUT_VARIABLE _echo_git_revision OUTPUT_STRIP_TRAILING_WHITESPACE
      ERROR_QUIET RESULT_VARIABLE _echo_git_result)
    if(_echo_git_result EQUAL 0)
      set(ECHO_SOURCE_REVISION "${_echo_git_revision}")
      execute_process(COMMAND "${GIT_EXECUTABLE}" diff-index --quiet HEAD --
        WORKING_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/.."
        RESULT_VARIABLE _echo_dirty ERROR_QUIET)
      if(NOT _echo_dirty EQUAL 0)
        string(APPEND ECHO_SOURCE_REVISION "-dirty")
      endif()
    endif()
  endif()
endif()
