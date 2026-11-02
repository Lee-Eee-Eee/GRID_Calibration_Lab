# 处理与数据指南

[返回项目首页](../README.md) · [工作台操作](../workbench/README.md)

## TB 与 EC

TB 在固定入射能量下测量温度、偏压对谱峰位置的影响；EC 采用 TB 参数进行温偏校正，再利用已知能线建立能量刻度。

以下层级适用于 `cali_proc@0.5` 的 14B / 15B 预设。旧试算继续使用其保存的七层配置。

| 层级 | 处理 | 主要产物 |
| --- | --- | --- |
| EXP | 按 XML 解包完整 SCI / HK，保留 CRC 及原生字段。 | `events.parquet`、`telemetry.parquet`、`issues.parquet`、`meta.json`。 |
| TB / EC L0 | 清洗有效 CRC；保留完整事件与独立 HK 时间轴。 | 同上，尚未做分析选段。 |
| TB L1 | 按工况和通道选择谱峰、拟合窗口与单背景模型。 | 峰位、误差、诊断及拟合图。 |
| TB L2 | 将多个工况的峰位拟合为温偏增益曲面。 | 每通道曲面参数及残差图。 |
| EC corr | 独立选择 SCI / HK，按角色实测温偏和指定 TB 包校正幅度。 | 中间计算与模型路径、哈希绑定。 |
| EC L1 | 对已知能线拟合峰位，多峰源使用独立窗口。 | 按能线组织的谱、参数和选段图。 |
| EC L2 | 分段 ODR 能量关系和分辨率。 | 带时间戳的逐通道参数及图表。 |

TB L1 和 EC L1 保留候选寻峰、拖窗、质量诊断及局部重拟合。默认分析窗口和背景模型来自确认 YAML；Co60 的 `gaus` 背景是额外高斯分量。分箱默认 4 ADC，TB 为 Counts/ADC，EC 为 Counts/s/ADC；低计数误差采用配置中的 Gehrels 规则，协方差不按残差重缩放。

14B / 15B 使用包级 `timestamp1 / 100 MHz`，不把 SCI 与 HK 时钟混用。EC 对信号和本底分别按当前物理通道的首末跨度归一化，检测时间倒退与超过 10 s 的间断。X 射线本底槽位按 `[1,2,3,0]` 取文件，再从该文件选择相同物理通道。配置、时间依据和选择范围均写入分析包。

## 工作区与输出

工作区是数据、已有产品和试算的组织位置，可以放在代码仓库之外。启动前创建工作区目录：

```powershell
New-Item -ItemType Directory -Force ../grid-workspace | Out-Null
uv run --extra workbench grid-workbench --workspace ../grid-workspace --port 8765
```

在界面选择已有数据和产品目录，创建独立试算。试算结果与运行记录放在 `runs/<run_id>/`；原始输入与历史产品作为读取来源。

```text
grid-workspace/
└── runs/
    ├── _presets/          # 载荷读取预设与版本
    ├── _sources/          # 已索引的数据目录
    └── <run_id>/
        ├── products/      # 本次试算的分层结果
        ├── jobs/          # 每次任务的输入、配置与输出记录
        └── ...            # 试算状态与人工修订
```

新版规范产物与工作台操作索引同时保存，原始事件不会为每次拖窗重复解包：

```text
products/<payload>/
├── EXP/<group>/<acquisition>/
│   └── events.parquet, telemetry.parquet, issues.parquet, meta.json
├── <tb|ec>/L0/<acquisition>/
│   └── events.parquet, hk.parquet, meta.json
├── <tb|ec>/L1/<analysis>/
│   └── points.json, spectra.parquet, meta.json, fit.png, selection*.png
└── <tb|ec>/L2/<timestamp>/
    └── 模型 JSON, companion meta.json, 诊断图
```

L1 的 `points.json` 以通道为键，参数误差使用 `*_std`，高斯幅度输出高度，EC 分辨率为比例。TB L2 为 `<timestamp>_temp_bias_fit.json`，EC L2 分通道输出。规范详见 [新数据规范对照](cali_proc_format_20261102.md)，实测结果见 [适配验收](contract_migration_validation.md)。

旧处理器内部操作 ID 和 `json/`、`parquet/` 索引继续支持已有界面与脚本。新规范接口的 `contract_layer=EC_corr/EC_L1/EC_L2` 分别映射内部 `EC_L1/EC_L2/EC_L3`，不会把两种编号混作同一接口。共享产品导入只创建小型读取索引，谱表留在原目录。

## 命令行运行

`grid-calib` 保留旧版配置，接受 `TB_L0`、`TB_L1`、`TB_L2`、`EC_L0`、`EC_L1`、`EC_L2`、`EC_L3`。下方示例属于该入口。新规范通过工作台或 [服务器准备工具](server_workbench.md) 加载确认文件表和四份分析配置；服务器 14B 对应旧原始目录 03，15B 对应 02，显式记录 `source_mapping`，不会自动重命名旧试算。

从源码可编辑安装时，默认 14B / 15B 原始目录相对于仓库上一级：

```text
14、15B标定数据/
├── 02/                    # 14B
│   ├── Temp_Vbias/
│   ├── X_new/
│   └── source/
└── 03/                    # 15B，具体子目录见载荷配置
```

在仓库根目录运行：

```bash
uv run grid-calib 14B TB_L0 --products ../products
uv run grid-calib 14B TB_L1 --products ../products
uv run grid-calib 14B TB_L2 --products ../products
```

修改某个工况的窗口后，可以只重跑对应 TB L1，再更新曲面：

```bash
uv run grid-calib 15B TB_L1 --products ../products --only m20C-287-106,0C-285-137
uv run grid-calib 15B TB_L2 --products ../products
```

`--only` 接原始 stem，只用于 TB L1。EC L1 的 CLI 会选择当前输出目录中最新的 TB L2 参数；需要明确指定另一次标定的 TB 版本时，使用工作台或 Python API。

## Python API

```python
from pathlib import Path
from calibration_lib.pipeline import CalibrationPipeline

pipeline = CalibrationPipeline("14B", Path("../products"))
tb_results = pipeline.run_tb()
ec_results = pipeline.run_ec()
```

EC 也可以显式指定 TB 参数：

```python
pipeline.run_ec(tb_l2_json_path=Path("../parameters/selected_TB.l2.json"))
```

## TB 曲面模型

```text
G(T, V) = G0 · (V − k·T − V0)² · (−T² + b·T + c)
```

| 参数 | 初值 | 边界 |
| --- | ---: | --- |
| G0 | 0.012 | [1e−5, 100] |
| k | 0.0184 | [0.001, 0.05] |
| V0 | 24.11 | [23, 25] |
| b | 54.31 | [−500, 500] |
| c | 23459.83 | [1e3, 1e8] |

表中是旧默认初值。新规范从 `tb_l2.yaml` 读取各通道初值、边界和拟合方法，使用 `1/center_std` 权重、`scale_covar=False`。曲面同时输出约化 χ²、RMSE 和 MAPE。EC L2 依 `ec_l2.yaml` 做 `E=f(ADC)` 分段 ODR，分辨率为 `sqrt(aE²+bE+c)/E`。具体实现见 [TB L2](../tb/processors/L2_processor.py) 和 [EC 聚合处理器](../ec/processors/L3_processor.py)。

## 只读产物总览

已有产品还可以生成独立 HTML 供浏览：

```bash
uv run python tools/flow_overview.py --products ../products --out ../outputs/calibration_flow.html --check
```

该工具扫描产品目录并检查文件链接，适合查看历史结果。需要执行计算、调整窗口或保存修订时，使用工作台。
