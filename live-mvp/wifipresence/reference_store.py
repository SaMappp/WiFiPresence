"""Store only the derived empty-reference parameters on the operator's PC."""

import json
import os
from pathlib import Path
import tempfile

import numpy as np

from .empty_reference import EmptyReference, MIN_REFERENCE_WINDOWS


class ReferenceStore:
    def __init__(self, path=None):
        home = Path(os.environ['LOCALAPPDATA']) if os.environ.get('LOCALAPPDATA') else Path.home() / '.local' / 'share'
        self.path = Path(path) if path is not None else home / 'WiFiPresence' / 'empty-reference-v1.json'

    def load(self, key):
        try:
            if self.path.stat().st_size > 8192:
                return None
            document = json.loads(self.path.read_text(encoding='utf-8'))
            if document['version'] != 1 or document['key'] != key:
                return None
            centers = tuple(np.asarray(center, dtype=float) for center in document['centers'])
            radii = np.asarray(document['radii'], dtype=float)
            count = document['window_count']
            if (tuple(center.shape for center in centers) != ((8,), (8,), (10,))
                    or not all(np.isfinite(center).all() for center in centers)
                    or radii.shape != (3,) or not np.isfinite(radii).all() or not (radii >= .025).all()
                    or type(count) is not int or not MIN_REFERENCE_WINDOWS <= count <= 100):
                return None
            return EmptyReference(centers, tuple(radii), count)
        except (OSError, KeyError, TypeError, ValueError, OverflowError):
            return None

    def save(self, key, reference):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {'version': 1, 'key': key, 'centers': [center.tolist() for center in reference.centers],
                'radii': list(reference.radii), 'window_count': reference.window_count}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='.empty-reference-', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, allow_nan=False)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
