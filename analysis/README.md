# Analytic reconstruction counts

English | [Chinese](README_cn.md)

These standalone scripts model sequential decoding, hierarchical reference
closures, DDRA and a fixed I/P GOP=2 baseline. They use the Python standard
library and do not need a video dataset, a wheel, or cloud credentials.

```bash
python -m unittest discover -s analysis -p 'test_*.py' -v
python analysis/decode_amplification.py --output-dir outputs/analysis
```

The tests check exact phase expectations, independently derived small graphs,
shared anchors, large strides and integer intra periods. These are idealized
graph models, not measurements of FFmpeg packet submissions or runtime latency.
In particular, a theoretical schedule that skips untouched intervals is distinct
from the native decoder's state-only traversal of a target-covering segment.
