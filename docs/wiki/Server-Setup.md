# 服务器安装与浏览器连接

工作台的 Python 服务和计算运行在 GRID 服务器，浏览器在自己的电脑上打开。每位成员使用自己的服务器账号、环境和工作区，输入数据指向共享目录。

下文 `grid` 是本机配置的 SSH 别名，应连接到本人的服务器账号。先在本地终端执行：

```bash
ssh grid
```

成功登录后，在服务器终端执行后续安装命令。

## 1. 在个人目录安装

需要 Python 3.10 或更新版本；建议使用已有的 Python 3.11–3.13 环境。

```bash
LAB_HOME="$HOME/calibration-workbench"
mkdir -p "$LAB_HOME/src" "$LAB_HOME/workspace"
git clone --branch feat/calibration-workbench \
  https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab.git \
  "$LAB_HOME/src/calibration_lib"
python3 -m venv "$LAB_HOME/.venv"
"$LAB_HOME/.venv/bin/python" -m pip install --upgrade pip
"$LAB_HOME/.venv/bin/python" -m pip install "$LAB_HOME/src/calibration_lib[workbench]"
```

仓库名称是 **GRID_Calibration_Lab**，Python 导入名仍为 `calibration_lib`，安装包名仍为 `grid-calibration-lib`。上述命令把代码安装到个人虚拟环境中，运行不依赖共享项目目录的写权限。

确认安装：

```bash
"$LAB_HOME/.venv/bin/python" -c "import calibration_lib; print(calibration_lib.__file__)"
```

输出应位于自己的 `$HOME/calibration-workbench/.venv/` 下。

## 2. 登记共享数据

当前 14B / 15B 的共享根目录为 `/lime/GRID/calibration/cali_data`。在启动工作台服务前执行：

```bash
"$LAB_HOME/.venv/bin/python" -m calibration_lib.tools.server_workspace \
  --workspace "$LAB_HOME/workspace" \
  --data-root /lime/GRID/calibration/cali_data \
  --create-runs
```

此命令完成以下准备：

- 在个人工作区建立 `raw` 和 `shared-products` 软链接。
- 登记默认数据 / 产品入口与确认的 14B / 15B 读取预设。
- 建立可打开的试算；重复执行会保留已选配置版本并复用对应来源的已有试算。

目录关系如下：

```text
~/calibration-workbench/
├── src/calibration_lib/        # 个人代码检出
├── .venv/                     # 个人 Python 环境
└── workspace/
    ├── raw -> /lime/GRID/calibration/cali_data/raw
    ├── shared-products -> /lime/GRID/calibration/cali_data
    └── runs/
        ├── datasets.json
        ├── _presets/          # 可复用读取预设及版本
        ├── _index/            # 数据、试算和产品关联
        └── <run_id>/          # 每份试算的配置、任务与结果
```

链接只连接服务器上的路径，原始事件不必下载到本地。选择输入目录、预设和已有结果的方法见[数据与读取预设](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/Data-and-Presets)。

准备命令使用仓库内的确认配置。其他成员个人工作区中后来发布的配置版本不会自动出现在自己的预设列表；15B Co60 双峰配置的建立方法见[EC 标定](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/EC-Calibration)。

## 3. 启动个人服务

同一台服务器的监听端口需要区分。与其他成员约定一个可用端口，下面以 **18780** 为例：

若希望 SSH 断开后仍保留计算，先进入个人 `tmux` 会话：

```bash
tmux new -s grid-calibration
```

在该会话内执行下面的启动命令；也可以直接在普通 SSH 终端执行：

```bash
LAB_HOME="$HOME/calibration-workbench"
LAB_PORT=18780
"$LAB_HOME/.venv/bin/python" -m calibration_lib.workbench.server \
  --workspace "$LAB_HOME/workspace" --port "$LAB_PORT"
```

终端出现服务器地址和 Workspace 路径后，服务已启动。普通终端方式需保持连接；使用 `tmux` 时，按 `Ctrl+B`，再按 `D` 分离会话，以后用 `tmux attach -t grid-calibration` 返回。每个工作区使用一个服务进程。

若提示端口已占用，换一个 `LAB_PORT` 后重新启动；若提示此工作区已有 Workbench 服务，连接已有服务即可。

## 4. 在本地建立 SSH 转发

在自己的电脑上另开一个终端，而不是在服务器终端中执行：

```bash
ssh -N -L 18780:127.0.0.1:18780 -o ServerAliveInterval=20 grid
```

保持终端打开，在浏览器访问 **http://127.0.0.1:18780/**。

`-L` 的三个部分分别是：本地端口、服务器上的目标地址、服务器服务端口。若服务器实际使用 18766，而自己的本地 18780 已占用，可以改为：

```bash
ssh -N -L 28765:127.0.0.1:18766 -o ServerAliveInterval=20 grid
```

此时打开 **http://127.0.0.1:28765/**。服务器启动参数使用的是 18766。

浏览器关闭或 SSH 转发断开后，保存在服务器个人目录中的结果仍可恢复。重新建立转发，再打开相同工作区的服务即可继续。

## 5. 开始操作与日常更新

进入界面后，先选择载荷与试算。要使用自己的数据，打开顶部“选择数据 / 产品”，按[数据与读取预设](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/Data-and-Presets)登记目录、检查配置并创建试算。

更新版本时，在自己的服务完成当前任务后退出服务，再在服务器执行：

```bash
LAB_HOME="$HOME/calibration-workbench"
git -C "$LAB_HOME/src/calibration_lib" pull --ff-only origin feat/calibration-workbench
"$LAB_HOME/.venv/bin/python" -m pip install --upgrade "$LAB_HOME/src/calibration_lib[workbench]"
```

随后用原来的 `--workspace` 和端口重启。已有试算保存在该工作区的 `runs/` 下。

若只需重建个人目录索引，可执行：

```bash
LAB_HOME="$HOME/calibration-workbench"
"$LAB_HOME/.venv/bin/python" -m calibration_lib.tools.server_workspace \
  --workspace "$LAB_HOME/workspace" --refresh-index
```

该操作从个人状态文件重建来源与试算关联，不启动计算。

下一步：[数据与读取预设](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/Data-and-Presets) · [TB 标定](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/TB-Calibration) · [EC 标定](https://github.com/Lee-Eee-Eee/GRID_Calibration_Lab/wiki/EC-Calibration)
