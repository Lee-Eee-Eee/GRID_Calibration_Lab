# 在 GRID 上计算，在本地操作工作台

数据、Python 处理器和试算结果放在服务器，浏览器通过 SSH 转发访问。普通软链接只连接同一台机器的路径；这里在服务器私有工作区建立链接，Windows 无需挂载或复制原始数据。

## 当前部署

| 内容 | 位置 |
| --- | --- |
| 服务器 | 本机 SSH 配置中的 `grid` |
| 共享数据 | `/lime/GRID/calibration/cali_data` |
| 14B / 15B 原始文件入口 | 共享数据下的 `raw/14B`、`raw/15B` |
| 私有工作区 | `~/calibration-workbench/workspace` |
| 私有环境 | `~/calibration-workbench/.venv` |
| 本地访问地址 | `http://127.0.0.1:18765/` |

服务器工作区中的 `raw` 指向共享 `cali_data/raw`，`shared-products` 指向共享 `cali_data`。新的 EXP、L0、分析结果和配置修订写入私有工作区的 `runs/`。共享项目目录无需写权限。

本地建立转发，并保持这个终端运行：

```powershell
ssh -N -L 18765:127.0.0.1:18765 -o ServerAliveInterval=20 grid
```

打开 `http://127.0.0.1:18765/`。SSH 断线后重新执行上述命令；服务和已保存结果仍留在服务器。

## 准备新部署

将本库构建的 wheel 上传到自己的服务器目录，安装到私有虚拟环境：

```bash
python3 -m venv ~/calibration-workbench/.venv
~/calibration-workbench/.venv/bin/python -m pip install './grid_calibration_lib-0.1.0-py3-none-any.whl[workbench]'
```

准备数据链接、14B/15B 配置预设及可继续操作的试算；此命令在服务启动前运行。再次执行会保留已选择的配置版本与已有试算：

```bash
~/calibration-workbench/.venv/bin/python -m calibration_lib.tools.server_workspace \
  --workspace ~/calibration-workbench/workspace \
  --data-root /lime/GRID/calibration/cali_data --create-runs
```

启动服务：

```bash
~/calibration-workbench/.venv/bin/python -m calibration_lib.workbench.server \
  --workspace ~/calibration-workbench/workspace --port 18765
```

服务绑定服务器的 `127.0.0.1`；本地 SSH 隧道负责转发 HTTP。无需 X11 转发，也无需 SSHFS。长期运行可以把上述服务命令放入个人 tmux 会话。

## 新规范配置与计算

“解包与读取设置 → 分析配置”可以载入对应载荷的文件表和四份配置，编辑选段、模型、能线、窗口、分段及参考条件；XML 和字段换算保留原入口。配置仍可另存为预设、保存新版本及绑定目录。每个试算保存四份 YAML、完整 profile 和 XML 快照。

服务器新规范的载荷命名以文件表为准：14B 指向旧 `14B15B/03` 数据，15B 指向旧 `14B15B/02`。已有本地试算的编号不自动重命名，新预设记录明确的来源映射。

新试算的界面层级为 TB L0–L2、EC L0 / corr / L1 / L2。解包操作同时生成 EXP 和清洗后的 L0，均保存在个人试算下。多峰能线表、拖窗、单通道重拟合和屏蔽点继续使用现有交互。

原有七个处理器的内部操作标识保留，方便既有试算继续运行。新接口提交可以明确使用规范层名：

```json
{"contract_layer": "EC_L1", "point_id": "当前能线ID", "channel": 0}
```

`contract_layer` 接受 `TB_L0`、`TB_L1`、`TB_L2`、`EC_L0`、`EC_corr`、`EC_L1`、`EC_L2`。试算详情的 `contract_stages` 给出与底层处理操作的映射。

选择新格式产品目录时，工作台生成自己的小型 JSON 读取索引，直接读取共享的 `spectra.parquet`。它不会为浏览谱而复制整套事件表。

## 共享来源与个人产品索引

个人工作区保存以下小型目录表，所有路径都位于自己的 `runs/`：

| 索引 | 内容 |
| --- | --- |
| `dataset_locations.json` | 默认共享 raw 和历史产品入口；重启、重新扫描仍使用服务器路径 |
| `datasets.json` | 目录配对、载荷编号、数据集 ID 和来源 ID |
| `_presets/index.json` | XML、字段换算、文件规则和分析 YAML 的版本及目录关联 |
| `_index/runs/<run_id>.json` | 个人产品入口、配置版本、所用 TB 包、EC 依赖、最近处理状态 |
| `_index/workspace.json` | 显式重建时的总索引快照 |

来源 ID 对应规范化的原始数据目录；改显示编号、换历史产品或创建新配置版本，不会把同一批采集拆成不同来源。数据集仍保留已有 ID。每个来源可以关联多份个人试算，工作台可列出这些试算继续操作；共享历史产品入口保持独立。

每次保存试算只更新该试算的小型索引，不遍历事件表或图像。服务启动时由个人 `state.json` 重建索引；列表读取无需扫描共享数据。产品预览、计算记录沿用工作台已有文件入口。API `GET /api/workspace-index` 提供数据来源、配置版本与个人产品的关联。

也可在服务运行期间重建个人索引，此命令不启动处理器、不改变任务状态：

```bash
~/calibration-workbench/.venv/bin/python -m calibration_lib.tools.server_workspace \
  --workspace ~/calibration-workbench/workspace --refresh-index
```

格式与算法细节见 [规范对照](cali_proc_format_20261102.md)；回归结果见 [新规范验收](contract_migration_validation.md)。
