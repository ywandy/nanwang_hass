# 南网在线 · Home Assistant 集成

把南方电网「南网在线」（[95598.csg.cn](https://95598.csg.cn/)）的余额、电量、电费接入 Home Assistant，并把逐日用电量按真实日期写进长期统计，能直接用在**能量面板**里。

- 走 Web 端接口（`/ucs/ma/wt`），**只调用查询接口**：不缴费、不绑定、不改资料，也不破解图形验证码。
- 接口字段已于 2026-11-12 用真实账号（广州居民户 + 充电桩户）验证。
- 不需要额外安装 Python 依赖（只用 Home Assistant 自带的 `aiohttp`、`cryptography`）。

> 非官方项目。接口和加密参数来自对官方 Web 前端的静态还原（见上级目录 `../poc`、`../findings`），官方改版后随时可能失效。

## 功能一览

- 短信验证码登录，或直接导入浏览器里的登录 token
- 一个账号下所有绑定户号自动发现，每个户号一个设备、11 个传感器
- 每天 0 点、12 点固定刷新，不做频繁轮询
- 每次刷新都重新同步最近 15 天的逐日电量（跨月也一样），首次安装回填 6 个月
- token 失效时自动提示重新认证，网关下发新 token 时自动保存
- 诊断信息一键导出，敏感字段自动打码

## 要求

- Home Assistant **2025.6** 或更新（已在 2025.6.0、2025.9.4、2026.2.3 上验证）
- 需要启用 `recorder`（默认就是开的），否则只有传感器、没有能量面板统计

## 安装

从 Home Assistant 2026.11 起，HACS 内置进 HA，改名为 **Marketplace**（设置 → Marketplace）。两者读的是同一份仓库列表，安装方式基本一样。

**一键添加**（HACS 和 Marketplace 都能用）：

[![打开 Home Assistant 并添加本仓库](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ywandy&repository=nanwang_hass&category=integration)

**HA 2026.10 及以前（HACS）**

1. HACS → 右上角 ⋮ → 自定义存储库（Custom repositories）
2. 填 `https://github.com/ywandy/nanwang_hass`，类别选 **Integration**，添加
3. 在 HACS 里搜索「南网在线」并下载，然后重启 Home Assistant

**HA 2026.11 及以后（Marketplace）**

用上面的一键链接打开，Marketplace 会提示先把本仓库添加为自定义仓库，确认后下载并重启。进入 HACS 默认列表后，可以直接在 Marketplace 里搜索「南网在线」。

**手动**

把 `custom_components/nanwang` 整个目录复制到 HA 配置目录下的 `custom_components/`（最终路径 `config/custom_components/nanwang/manifest.json`），然后重启。

## 配置

设置 → 设备与服务 → 添加集成 → 搜索「南网在线」，二选一：

### 方式一：手机号 + 短信验证码

1. 填手机号，选所在地区（只影响登录接口，查数据时会自动用户号自己的地区）
2. 提交后会收到一条登录验证码，填入即可

### 方式二：导入浏览器 token

1. 用电脑浏览器登录 https://95598.csg.cn/
2. 打开开发者工具 → Application（应用）→ Session Storage → `https://95598.csg.cn`
3. 复制 `token` 的值；`custnumber` 可选，有就一起填
4. 粘贴到集成表单里

### 重新登录

token 失效时（接口返回 `sta=04`），集成会在下一次刷新时进入「需要重新认证」状态：

1. 设置 → 设备与服务，顶部会出现「南网在线」的重新认证卡片（通知里也有提示）
2. 点 **重新认证**，同样可以选短信登录或导入 token；短信登录会自动带出原来的手机号
3. 成功后集成自动重新加载并立刻拉一次数据，实体和历史统计都保留

失效期间传感器保持上一次的值。

> 同一个 token 如果同时给别的工具用（例如 `../poc/nw.py`），在那边重新登录可能会把 HA 这边顶下线。

## 刷新机制

- HA 启动（或集成重新加载）时拉一次
- 之后每天当地时间 **0:00、12:00** 各刷新一次
- 官方逐日数据一般滞后 1~2 天，所以「最近一日电量」通常是前一天或前两天的数

每次刷新对每个户号的请求：户号列表、余额、年度分析（年初会多查一次上一年）、当月逐日数据（月初 15 天内会多查上个月）。计量点只在第一次查询后缓存。

## 设备与实体

每个绑定户号一个设备：名称 `南网 <户号>`，型号显示「地区 · 类型」，例如「广州 · 居民」「广州 · 充电桩」。

实体 ID 形如 `sensor.nan_wang_<户号>_<后缀>`：

| 实体 | 实体 ID 后缀 | 单位 | 数据来源 | 属性 |
|---|---|---|---|---|
| 电费余额 | `balance` | CNY | `queryUserAccountNumberSurplus` | |
| 欠费金额 | `arrears` | CNY | 同上（缺失时回退到户号列表的 `totalOwnedFee`） | |
| 锁定余额 | `locked_balance` | CNY | 同上 | |
| 今年电量 | `energy_this_year` | kWh | `getAnalyzeFeeDetails` | 上次重置（每年 1 月 1 日） |
| 今年电费 | `cost_this_year` | CNY | 同上 | 上次重置（每年 1 月 1 日） |
| 最近账单月电量 | `latest_billed_month_energy` | kWh | 同上 | 账单月份，如 `2026-10` |
| 最近账单月电费 | `latest_billed_month_cost` | CNY | 同上 | 账单月份 |
| 最近账单月均价 | `latest_billed_month_unit_price` | CNY/kWh | 官方电费 ÷ 官方电量 | 账单月份 |
| 本月电量 | `energy_this_month` | kWh | `queryDayElectricByMPoint` | 月份，如 `2026-11`；上次重置（每月 1 日） |
| 本月预估电费 | `estimated_cost_this_month` | CNY | 本月电量 × 最近账单月均价 | 参考均价、均价取自 |
| 最近一日电量 | `latest_day_energy` | kWh | `queryDayElectricByMPoint` | 数据日期 |

说明：

- 余额类按计量点求和；一个户号有多个计量点时，电量类只取第一个计量点。
- 「今年电量」「今年电费」「本月电量」是 `state_class: total` 并带 `last_reset`，HA 会自动编译长期统计，点开实体看到的是统计曲线；其余实体是普通历史折线。一天只刷新两次，所以曲线是台阶状的。
- 实体名和属性名都有简体中文、英文翻译，跟随 HA 的语言设置。

## 能量面板

逐日电量会写进外部长期统计 **`nanwang:<户号>_energy`**（显示名「南网 <户号> 用电量」），每天一条、记在当天 0 点：

- 首次安装：回填最近 **6 个月**
- 之后每次刷新：重写最近 **15 天**，迟到或被官方修正的数据会补上
- HA 停机造成断档：从最后一条统计那天开始补，最多往回 **12 个月**

添加方法：设置 → 仪表盘 → 能量 → 电网用电量 → 添加，选择「南网 <户号> 用电量」。

> 不要选「本月电量」传感器。官方数据滞后，它的增量会被记在「拉到数据的那一刻」而不是实际用电的那天。

## 数据口径与限制

- **没有官方的当月/逐日电费**：逐日接口只返回电量，另外两条官方路径实测也拿不到金额。「本月预估电费」是按最近账单月均价估算的，**不是账单口径**。
- **账单有滞后**：当月账单要到下个月才出，所以「最近账单月」通常是上个月；1 月份当年还没有账单时会回查上一年。
- **余额**：后付费户号接口返回的余额、欠费可能都是 0，以接口为准。
- **没有数据的户号**（例如长期不用电的充电桩，接口返回 `sta=09`）：电量类显示 0 或「未知」，只记 debug 日志，不刷警告。
- **新绑定的户号**需要重新加载集成（设置 → 设备与服务 → 南网在线 → ⋮ → 重新加载）才会出现。

## 隐私与安全

- token、custNumber、cookie、手机号保存在 HA 的配置项里（`.storage/core.config_entries`），和其它集成的凭据一样。
- 只发查询请求；登录时的发短信、登录两个请求不会自动重试。
- 下载诊断时，token、cookie、手机号、户号、户名、地址、计量点号、结算账号等都会打码。

## 故障排查

**下载诊断**：设置 → 设备与服务 → 南网在线 → ⋮ → 下载诊断。里面是脱敏后的接口原始返回，方便核对字段。

**打开调试日志**（`configuration.yaml`）：

```yaml
logger:
  logs:
    custom_components.nanwang: debug
```

**常见状态码**

| sta | 含义 | 处理 |
|---|---|---|
| `04` | 未登录 / token 失效 | 按「重新登录」操作 |
| `0408` | 账号在别处登录被踢下线 | 重新登录 |
| `09` | 该计量点没有数据 | 正常现象（如充电桩），无需处理 |
| `0500` | 账号没有绑定户号 | 先在南网在线 App / 网站绑定户号 |
| `02` | 系统异常 | 多为官方临时故障；持续出现可能是前端改版、加密参数变了 |
| `0009` / `0005` / `0006` | 验证码太频繁 / 输错次数过多 / 已失效 | 按提示等待后重新获取 |
| `0001003` | 短信验证码错误 | 检查验证码 |

## 开发

**目录结构**

```
custom_components/nanwang/
├── __init__.py       # 加载集成、共享会话、0 点/12 点定时刷新
├── api.py            # 协议层：AES 加解密、token 续期、各查询接口（不依赖 HA，可单独测试）
├── config_flow.py    # 短信登录 / 导入 token / 重新认证
├── const.py          # 常量：地区编码、刷新时刻、同步天数等
├── coordinator.py    # 拉取所有户号数据，同步最近 15 天到长期统计
├── statistics.py     # 外部长期统计的写入与窗口计算
├── sensor.py         # 11 个传感器
├── diagnostics.py    # 脱敏诊断
└── translations/     # zh-Hans、en
```

常用参数在 `const.py`：`REFRESH_HOURS`（刷新时刻）、`RECENT_DAYS`（每次同步天数）、`STATS_INITIAL_MONTHS`（首次回填月数）、`STATS_MAX_MONTHS`（断档最多补几个月）。

**跑测试**（全部离线，不连官方服务器）

```bash
pip install -r requirements_test.txt
pytest -q
```

共 23 个测试，覆盖：

- `tests/test_api.py`：本地 mock 网关走真实 HTTP，验证 AES 加解密（与 POC 的已验证向量逐字节一致）、token 续期、不采纳游客 token、计量点字段回退、余额接口
- `tests/test_config_flow.py`：短信登录、导入 token、错误提示、无户号、重新认证
- `tests/test_init.py`：传感器数值、固定时刻刷新、跨月取最近一日、无数据户号、token 回写、诊断脱敏、属性翻译齐全
- `tests/test_statistics.py`：内存 recorder 验证回填、15 天重写、断档补数

已在 HA 2025.6.0 与 2026.2.3 上通过。长期统计的 `unit_class` 字段在新版 HA 必填、在 2025.6 会报错，代码会按当前版本自动决定传不传。

**校验 manifest / 翻译**（hassfest，需要 Docker）

```bash
docker run --rm --platform linux/amd64 -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest
```

推送到 GitHub 后，`.github/workflows/validate.yml` 会自动跑 hassfest、HACS 校验和测试。

## 发布到 HACS / Marketplace

HA 2026.11 起 HACS 成了内置的 Marketplace，但**上架流程没变**：默认列表仍然是 GitHub 上的 `hacs/default` 仓库，老版本的 HACS 和新版的 Marketplace 读同一份数据，上架一次两边都能搜到。

### 1. 准备仓库

- [ ] 在**个人账号**下建公开的 GitHub 仓库（组织账号提不了上架 PR）。填好仓库描述和 topics（如 `home-assistant`、`hacs`、`homeassistant-integration`），并开启 Issues
- [x] `manifest.json`：`documentation`、`issue_tracker` 指向本仓库，`codeowners` 为 `["@ywandy"]`
- [x] LICENSE：MIT（HACS 要求 OSI 认可的许可证）
- [x] 图标：`custom_components/nanwang/brand/icon.png`（256×256，透明背景，裁掉留白），可选 `icon@2x.png`（512×512）、`dark_icon.png`、`logo.png`。HA 2026.3 起自定义集成直接用自带的图标，`home-assistant/brands` 已经不再接收自定义集成
- [x] 已删掉 `.github/workflows/validate.yml` 里的 `ignore: brands`：上架要求 HACS 校验**不跳过任何检查**也能通过
- [x] `hacs.json` 已带 `country: CN`（只面向特定国家的仓库必须写）

### 2. 让 CI 全绿，然后发正式 Release

- [ ] 推送后确认 GitHub Actions 里的 hassfest、HACS 校验、测试都通过
- [ ] 发一个**正式的 GitHub Release**（只打 tag 不算），版本号和 `manifest.json` 的 `version` 保持一致，例如 `v0.2.0`

到这一步，别人已经可以通过「自定义仓库」或上面的一键链接安装了。

### 3. 提交到默认列表（可选）

1. Fork [`hacs/default`](https://github.com/hacs/default)，从 `master` 新建分支
2. 在 `integration` 文件里按字母顺序加一行 `"ywandy/nanwang_hass"`
3. 用个人账号提 PR，模板要填完整：最新 Release 链接、一次**没有 ignore** 的 HACS 校验通过记录、一次 hassfest 通过记录
4. 等审核：官方说明新提交通常要排**几个月**。不要在 PR 里点名请人 review，会被直接关闭

只有仓库所有者或主要贡献者可以提交。用于测试 HA 内置集成预览版、或者覆盖内置集成的仓库不会被接受，本集成的域名是 `nanwang`，不会冲突。

### 用户这边

- HA 2026.10 及以前：继续用 HACS。HACS 本身已不再更新，但仓库列表照常可用
- HA 2026.11 及以后：升级时 HACS 会自动迁移到 Marketplace，已安装的集成和自定义仓库都会保留。新版不能再单独安装 HACS
- Marketplace 安装前会检查更多东西，比如 Release 声明的最低 HA 版本、域名不能和内置集成冲突；本集成在 `hacs.json` 声明了最低 2025.6.0

## 致谢

- 协议还原与真实账号验证：本仓库 `../poc/nw.py`、`../findings/poc-verification.md`
- 加密参数交叉验证参考了社区项目 `mintisan/home-assistant-shenzhen-utilities`、`CubicPill/china_southern_power_grid_stat`
