"""Rolling stability score sent live on the data channel (SCRUM-155, SCRUM-192).

PLACEHOLDER FORMULA ("demo-0"). The real v1 formula and weights are still to be
agreed with AllGoodSpeakers (SCRUM-190). What matters here is the *shape*:
an O(1)-per-sample rolling window that can run inside the UDP hot path
without ever blocking the event loop.

Metrics over the last `window` samples:
  * CoP sway speed (mm/s): path length of the centre of pressure / duration.
    The CoP is low-pass filtered first, otherwise load-cell noise alone adds a
    large fake path length at 100 Hz.
  * Tilt RMS (deg): root-mean-square of roll and pitch.

    score = clamp(100 - 1.0 * sway_speed - 10 * tilt_rms, 0, 100)
"""

from __future__ import annotations

import math
from collections import deque

SCORE_VERSION = "demo-0"


class RollingStability:
    def __init__(self, window: int, rate_hz: float, smoothing: float = 0.2):
        self.window = window
        self.dt = 1.0 / rate_hz
        self.alpha = smoothing          # EMA factor for the CoP low-pass
        self.points: deque[tuple[float, float]] = deque()   # filtered CoP
        self.tilts: deque[tuple[float, float]] = deque()    # (roll^2, pitch^2)
        self.segments: deque[float] = deque()                # |p[i] - p[i-1]|
        self.path = 0.0                 # running sum of segments
        self.tilt_sq = 0.0              # running sum of roll^2 + pitch^2
        self._ema: tuple[float, float] | None = None
        self._n_since_resync = 0

    def add(self, cop: tuple[float, float], roll: float, pitch: float) -> float:
        # 1. Low-pass the CoP.
        if self._ema is None:
            self._ema = cop
        else:
            a = self.alpha
            self._ema = (self._ema[0] + a * (cop[0] - self._ema[0]), self._ema[1] + a * (cop[1] - self._ema[1]))
        p = self._ema

        # 2. Push into the window, updating running sums instead of recomputing
        #    over all 200 samples (keeps the hot path O(1)).
        if self.points:
            seg = math.dist(p, self.points[-1])
            self.segments.append(seg)
            self.path += seg
        self.points.append(p)
        tsq = roll * roll + pitch * pitch
        self.tilts.append(tsq)
        self.tilt_sq += tsq

        # 3. Evict the oldest sample once the window is full.
        if len(self.points) > self.window:
            self.points.popleft()
            self.path -= self.segments.popleft()
            self.tilt_sq -= self.tilts.popleft()

        # Floating-point running sums drift slowly; resync now and then.
        self._n_since_resync += 1
        if self._n_since_resync >= 10_000:
            self._n_since_resync = 0
            self.path = sum(self.segments)
            self.tilt_sq = sum(self.tilts)

        return self.score()

    def score(self) -> float:
        n = len(self.points)
        if n < 2:
            return 100.0
        sway_speed = self.path / ((n - 1) * self.dt)                 # mm/s
        tilt_rms = math.sqrt(max(self.tilt_sq, 0.0) / (2 * n))       # deg, over both axes
        return max(0.0, min(100.0, 100.0 - 1.0 * sway_speed - 10.0 * tilt_rms))
