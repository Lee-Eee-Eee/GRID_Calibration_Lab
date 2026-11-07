# 工作台回归

从库仓库根目录运行：

```powershell
.venv/Scripts/python.exe -m pytest tests -q
node --test tests/frontend/*.test.cjs
```

真实浏览器测试使用独立临时工作区和回环地址的动态端口。它读取固定种子生成的小 Parquet，启动现有 HTTP handler、Workbench 和 TBL1Processor，加载生产 HTML、JS 和 Plotly；不连接运行中的工作台。

首次安装浏览器测试依赖：

```powershell
uv pip install --python .venv/Scripts/python.exe -e ".[dev,workbench,browser]"
.venv/Scripts/python.exe -m playwright install chromium
```

运行贯通回归：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_l1_review_browser.py --run-browser -q
```

也可使用本机 Chrome：

```powershell
$env:GRID_TEST_BROWSER_CHANNEL = "chrome"
.venv/Scripts/python.exe -m pytest tests/test_l1_review_browser.py --run-browser -q
```

浏览器由测试独立启动，保持无头运行。未传 `--run-browser` 时，该测试明确标为 skipped；显式启用后，缺依赖或浏览器会使测试失败。

贯通场景使用两个不同谱形的文件、四个可区分通道：

- CH3 待建模点经真实散点点击读取谱，保持 `model_pending`，没有高斯曲线和拟合作业。
- 缺温偏的文件从未定位工况列表进入，坐标保持 `null`，不进入散点图；读取请求和返回文件、通道一致。
- CH2 从首次坏窗失败，经过质量图重新选点，改窗局部重算并保存。CH0/CH1 已有真实拟合，CH0/CH1/CH3 的完整谱响应在操作前后保持一致。
- 成功后关闭并新建 Workbench，再用同一浏览器存储的新页面重开任务，核对点 ID、窗口、状态、参数和曲线。
- 再次坏窗失败后重复重建：最新尝试仍为失败；保留的旧曲线使用旧成功窗口，当前草稿仍为坏窗，两者分别核对。

`requests.json`、`acceptance.json`、`fitted.png` 和 `reopened.png` 写入 pytest 的临时测试目录。可用 `--basetemp=<新的测试目录>` 保留一轮证据。

已有测试的复用与新增：

| 位置 | 覆盖 |
| --- | --- |
| `test_tb_first_fit.py`（增强） | 复用 `l0`、`settled` 和真实坏窗/好窗流程，增加不同通道谱、缺 HK、质量图状态及两次 Workbench 重建断言 |
| `test_l1_review.py`（保留） | 待建模、缺坐标、三种失败来源的语义单测 |
| `test_workbench.py`（保留） | 配置修订重建恢复、HTTP handler 等既有测试 |
| `frontend/l1_quality.test.cjs`（新增两例） | 正常 Plotly 回调、缺坐标按钮走生产选点/绘谱函数；DOM、Plotly、API 仍为单测替身 |
| `frontend/workbench.test.cjs`（新增两例） | 最新失败优先于窗口待拟合；待建模不受其他通道失败影响 |
| `test_l1_review_browser.py`（新增一例） | 无替身的浏览器、HTTP、处理器、保存和重开贯通 |

浏览器驱动使用 [Playwright Python](https://playwright.dev/python/docs/library)，仅作为可选测试依赖。
