#pragma once
#include <QAtomicInt>
struct FramePolicy {
  inline static QAtomicInt s_currentGridCount{1};
};
