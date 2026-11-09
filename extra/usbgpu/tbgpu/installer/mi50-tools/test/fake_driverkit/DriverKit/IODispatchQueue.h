// Stands in for DriverKit's IODispatchQueue (IODispatchQueue.iig, SDK 25.5: "All queues are currently serial, executing one block at time FIFO
// order"; Create(name, options, priority, &queue), DispatchAsync_f, DispatchSync_f; the new object has retain count 1). The driver uses it for
// the MI50's bring-ups (DK-02). Inline mode (the default): a function runs as it is dispatched, as the bring-up ran inside Start before
// DK-02, so the older tests keep their meaning. Manual mode (g_queue_manual): dispatched functions wait until the test drains the queue or a
// DispatchSync_f runs, so a test sets the order of power changes, Stop and the queued bring-ups. Single-threaded, like the fake IOLock.
#pragma once
#include <DriverKit/IOLib.h>
#include <cstring>
#include <deque>
#include <utility>

typedef char IODispatchQueueName[256];          // OSObject.h:248
typedef void (*IODispatchFunction)(void *context);   // IODispatchQueue.h:41
inline bool g_queue_manual = false;
inline int g_queue_fail_create = 0, g_queues_made = 0, g_queue_runs = 0;
struct IODispatchQueue : OSObject {
  std::deque<std::pair<void *, IODispatchFunction>> pending;
  bool running = false;
  bool hold = false;   // "MI50 thermal": the watchdog's endless loop never runs inline; a test calls mi50::thermal_tick itself (Leon 2026-10-17)
  static kern_return_t Create(const IODispatchQueueName name, uint64_t options, uint64_t priority, IODispatchQueue **queue) {
    if (g_queue_fail_create || options || priority) return kIOReturnNoMemory;   // no options or priorities are defined (IODispatchQueue.iig)
    *queue = new IODispatchQueue; (*queue)->hold = !strcmp(name, "MI50 thermal"); g_queues_made++; return kIOReturnSuccess;
  }
  void drain() {
    if (running) return;   // a function dispatched from a running one runs after it, in this loop
    running = true;
    while (!pending.empty()) { auto w = pending.front(); pending.pop_front(); g_queue_runs++; w.second(w.first); }
    running = false;
  }
  void DispatchAsync_f(void *context, IODispatchFunction function) { pending.push_back({context, function}); if (!g_queue_manual && !hold) drain(); }
  void DispatchSync_f(void *context, IODispatchFunction function) {
    if (running) { fprintf(stderr, "FAIL DispatchSync_f on the queue it runs on (deadlock)\n"); abort(); }
    drain(); g_queue_runs++; function(context);
  }
  size_t queued() const { return pending.size(); }
};
