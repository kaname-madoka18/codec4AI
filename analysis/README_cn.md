# 理论重建数量分析

[English](README.md) | 简体中文

这些独立脚本对顺序解码、分层参考闭包、DDRA 和固定 I/P GOP=2 基线进行建模。
它们只使用 Python 标准库，不需要视频数据集、wheel 或云凭据。

```bash
python -m unittest discover -s analysis -p 'test_*.py' -v
python analysis/decode_amplification.py --output-dir outputs/analysis
```

测试检查精确的相位期望值、独立推导的小型图、共享锚点、大步长和整数帧内周期。
这些是理想化图模型，不是对 FFmpeg 数据包提交数量或运行时延迟的测量。
尤其需要区分两种行为：理论调度可以跳过未使用的区间，而原生解码器会在覆盖目标帧的
区段内执行仅更新状态的遍历。
