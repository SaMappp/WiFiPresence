"""Compare a CSI window with an operator-confirmed empty link in this session."""

from dataclasses import dataclass

import numpy as np


REFERENCE_SECONDS = 24
REFERENCE_CHECK_SECONDS = 5
MIN_REFERENCE_WINDOWS = 60
MIN_REFERENCE_WINDOWS_PER_HALF = 20
SUSTAINED_DEVIATION = 1.5


def confirm_sustained_presence(rows):
    """Resolve a borderline window only with separate, recent strong evidence."""
    current = rows[-1]
    if (current['reason'] != 'empty_reference_uncertain'
            or current['reference_distance'] <= SUSTAINED_DEVIATION):
        return
    # Windows overlap. Use the closest earlier window with zero overlap.
    for earlier in reversed(rows[:-1]):
        gap = current['start_s'] - earlier['end_s']
        if gap < -1e-8:
            continue
        if gap <= .25 + 1e-8 and earlier['reason'] == 'empty_reference_deviation':
            current.update(presence='PRESENT', reason='empty_reference_sustained')
        return


def groups(features):
    values = np.atleast_2d(features)
    bands = np.expm1(values[:, :8])
    shape = np.log1p(16 * bands / np.maximum(bands.mean(axis=1, keepdims=True), 1))
    return values[:, :8], shape, values[:, 8:18]


def distance(values, center):
    return np.sqrt(np.mean((values - center) ** 2, axis=1))


@dataclass(frozen=True)
class EmptyReference:
    centers: tuple
    radii: tuple
    window_count: int

    def matches_empty_check(self, rows):
        """Verify a saved baseline against a fresh operator-confirmed empty room."""
        labels = [self.predict(row['features'])[0] for row in rows if row['features'] is not None]
        return len(labels) >= 10 and 'PRESENT' not in labels and labels.count('EMPTY') >= .85 * len(labels)

    @classmethod
    def fit(cls, rows):
        eligible = [row for row in rows if row['features'] is not None]
        early = sum(row['start_s'] < REFERENCE_SECONDS / 2 for row in eligible)
        if (len(eligible) < MIN_REFERENCE_WINDOWS
                or min(early, len(eligible) - early) < MIN_REFERENCE_WINDOWS_PER_HALF):
            return None
        inputs = groups(np.asarray([row['features'] for row in eligible]))
        centers = tuple(np.median(values, axis=0) for values in inputs)
        radii = tuple(max(float(np.quantile(distance(values, center), .98)), .025)
                      for values, center in zip(inputs, centers))
        return cls(centers, radii, len(eligible))

    def predict(self, features):
        deviation = max(float(distance(values, center)[0]) / radius
                        for values, center, radius in zip(groups(features), self.centers, self.radii))
        if deviation > 1.8:
            return 'PRESENT', 'empty_reference_deviation', deviation
        if deviation < 1.15:
            return 'EMPTY', 'empty_reference_match', deviation
        return 'UNKNOWN', 'empty_reference_uncertain', deviation
