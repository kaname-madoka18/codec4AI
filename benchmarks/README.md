# 原研究脚本的最小迁移

这里基本复用原脚本，作为实验参考。本轮只做源码逻辑、语法和接口核查；
没有安装依赖、运行实验或保证当前机器能直接启动这些入口。
未新增本地 DataLoader 框架、测试套件或替代预处理协议。

| 文件 | 用途与保留配置 |
| --- | --- |
| `data_curate_loader_benchmark.py` | 主表八实现；四种采样；W32/B8、10 warmup +500 measured；另支持 3.3 的100-step |
| `calibrate_data_curate_quality.py` | 原30-episode质量标定、pooled-RGB PSNR、整数质量搜索与门禁 |
| `transcode_data_curate_formal.py` | 原三数据集正式转码、10个rank/每节点60CPU、JPEG/Lance/AV1、GOP32/B31 |
| `codec_ai_training_3_3_main_table_matrix.py` | 原CPU16的26配置，CPU8/32各一基线 |
| `run_codec_ai_training_3_3_main_table.py` | 原八实现矩阵runner，保留dry-run |
| `codec_ai_training_3_4_node_bandwidth.py` | 正式3.4：CPU16/W32/B8、continuous10、10+100步，20/50/100/200/400/800 MiB/s |
| `node_bandwidth_proxy_lib.sh` | 原3.4 Squid/proxychains配置函数 |
| `node_bandwidth_1h/` | 原一小时适配器和自己的代理helper；单独保留，不替代3.4正式配置 |

两个节点适配器均导入这里唯一的 `data_curate_loader_benchmark.py`。
原数据集抽样、转码设置、直接 Lance 读取、计时边界和报告结构基本保持。
预处理没有换成公开 API 的 GOP8/B7；原 GOP32/B31 定制编码器仍是外部条件。
原代理 helper 包含安装/配置函数，需结合目标系统使用；本轮未调用它们。
内部集群启动、监控和数据集 manifest 构造脚本未收录。

## 必要改动

`pyav_native` 先 demux 建立展示 PTS/关键帧表，再按目标所在 intra period 分组；
每组 backward keyframe seek 后向后顺序解码，取得该组目标后停止，直接跳到下一组。
索引构建在原 decode 计时内，无目标的中间区间不做图像解码。返回顺序和重复请求保留。
B帧重排和开放GOP的边界依赖由解码器处理；不是按平均FPS推算帧号。
输入限单视频流 H.264/HEVC MP4，需有效、唯一的 sample PTS；不支持的输入明确失败。

报告增加 `pyav_decode_strategy=keyframe_seek_intra_periods_v1`。三个入口的恢复检查
拒绝复用旧顺序PyAV结果；重新实验应使用新的输出根。PyAV所用的 `Packet.is_discard`
需相应版本支持，原环境记录的版本是18.1.0。

内部固定URI/个人凭据路径改为配置；跨区域bucket特判改为可选
`OSS_BUCKET_ENDPOINTS` JSON映射，其值为 `{ "bucket-name": "endpoint" }`。
输出仍遵循原OSS布局与不可变写入检查，没有增加本地存储适配。

主表及两个节点入口使用：

| 配置 | 含义 |
| --- | --- |
| `DATA_CURATE_MANIFEST_URI` | 已有原始 manifest 的OSS URI |
| `EXPECTED_MANIFEST_SHA256` | 原始 manifest 的字节校验值 |
| `DATA_CURATE_PREPROCESS_ROOT` | 已有预处理根目录 |
| `EXPECTED_PREPROCESS_SUCCESS_SHA256` | 预处理 `_SUCCESS.json` 校验值 |
| `DATA_CURATE_OUTPUT_ROOT` / `--output-root` | 新结果根目录 |
| `OSS_CREDENTIAL_FILE` / `--credential-file`、`OSS_ENDPOINT` / `--endpoint` | 原脚本访问配置；节点3.4入口要求显式CLI参数 |

质量标定和正式转码继续使用各自原CLI；固定内部输出根约束改为调用者显式给出的OSS根。
质量标定保留 `--seed-output-uri` 及来源SHA检查；正式转码只保留formal分支。
没有改变原数据集的内容、顺序、标定标记或编码器hash约束。

## 数据和依赖

原三数据集为 `realsource_world`、`robocasa`、`table30v2` 各1000个episode。
复用已有manifest，不重新构造；原始索引SHA256：

```text
062cd6c64c4177406c17e040a3694f2fea11558d4404b0f3d9d01780c495ad2e
```

原预处理marker SHA256：

```text
00fda734fda816d0638c56b1c02517535ed89a60ec34a17a70bef51d26bdd1da
```

这两份数据及其媒体并未随代码提供。主表即使选择native路径，也保留原有预处理marker检查。
需要自行取得授权数据并配置访问位置。

Python依赖包括 NumPy、Torch、oss2，以及所选后端的 PyAV/Decord/TorchCodec、
Lance/Arrow/Pillow。部分依赖仍在模块顶层导入，因此缺依赖时连 `--help` 也可能失败。
历史记录有 PyAV18.1.0、Decord0.6.0、TorchCodec0.7.0、pylance3.0.1；这是来源记录，
不是新验证的完整环境锁。Torch/TorchCodec/FFmpeg需匹配。
质量标定另需SVT-AV1及原GOP32/B31工具链；节点实验需Squid/proxychains与合适的CPU/内存配额。

仅标准库的矩阵文件可作为阅读入口：

```bash
python benchmarks/codec_ai_training_3_3_main_table_matrix.py --cpu-limit 16
```

实际运行的调用方式仍是 `python benchmarks/<原脚本名>.py ...`。
3.3 runner 的 `--benchmark-script` 指向同目录的主表文件。
没有收录私有集群runner，也没有为本机重新实现一套启动流程。

## 指标边界

Loading Time 为 DataLoader 等待，含IO/解码/调度/结果IPC；按数据集算mean/p95后宏平均。
Decord/TorchCodec的返回帧数与PyAV/Ours的解码工作量口径不同。
结果fingerprint不等于完整像素验证，cgroup CPU数值也依赖运行隔离范围。
3.4代理窗口包括warmup/prefetch至worker关闭；性能统计只用后续100步。
一小时版本默认每实现测至少3600秒，原脚本允许调整duration及探索带宽点位；不能与正式3.4混称。
