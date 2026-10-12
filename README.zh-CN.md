# AD35 HA Panel

让 AD35-S3 320×480 桌面控制屏接入 Home Assistant：在 HA 中添加屏幕、选择显示设备，并管理屏幕固件更新。

[Zemismart 官网](https://www.zemismart.com/) · [English](README.md) · [版本下载](https://github.com/zemismart-dev/ad35-ha-panel/releases/latest) · [更新记录](CHANGELOG.md)

## 关于 Zemismart

Zemismart 提供智能开关、窗帘及卷帘电机、照明、传感器和网关等智能家居产品。访问 [Zemismart 官网](https://www.zemismart.com/)了解产品。

[![在 HACS 中添加 AD35 HA Panel](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=zemismart-dev&repository=ad35-ha-panel&category=integration)

**[点击这里，通过 HACS 安装](https://my.home-assistant.io/redirect/hacs_repository/?owner=zemismart-dev&repository=ad35-ha-panel&category=integration)**

## 首次安装

先在自己的 Home Assistant 中[安装并配置 HACS](https://www.hacs.xyz/docs/use/)。本仓库以 **HACS 自定义仓库**方式安装，尚未收录到 HACS 默认仓库列表。

1. 点击上面的安装按钮，按提示打开自己的 HA，并添加此仓库。
2. 在 HACS 中打开 **AD35 HA Panel**，点击「下载」。
3. **重启 Home Assistant**。
4. 打开「设置 → 设备与服务」，配置发现的 AD35 屏幕；也可点击「添加集成」，搜索 **AD35 HA Panel**。

无需修改 `configuration.yaml`。一个 HA 可以添加多台屏幕，每台独立绑定和选择显示内容。

### 按钮打不开时

在 HACS 右上角「⋮ → 自定义仓库」填写：

| 项目 | 内容 |
| --- | --- |
| 仓库地址 | `https://github.com/zemismart-dev/ad35-ha-panel` |
| 类型 | 集成（Integration） |

点击「添加」，再搜索 **AD35 HA Panel**，下载并重启 HA。参见 [HACS 自定义仓库说明](https://www.hacs.xyz/docs/faq/custom_repositories/)。

### 不使用 HACS：手动安装

1. 从 [Releases](https://github.com/zemismart-dev/ad35-ha-panel/releases/latest) 下载附件 `ad35-ha-panel-integration-0.9.3.zip`，并解压。
2. 将其中的 `custom_components/ad35_ha_panel` 文件夹放到 HA 配置目录的 `custom_components` 下。最终应存在 `/config/custom_components/ad35_ha_panel/manifest.json`；HA Container 使用对应的配置挂载目录。不要多套一层文件夹。
3. 重启 HA，再到「设置 → 设备与服务」添加集成。

## 兼容性

| 项目 | 要求 |
| --- | --- |
| Home Assistant | Core **2026.8.0 或以上**；当前验证基线为 2026.8.0 |
| 设备 | AD35-S3、320×480 屏幕，运行配套 AD35 HA Panel 固件 |
| 建议固件 | **2.8.1 或以上**，包含 HA 授权续期修复 |
| 网络 | HA 与屏幕位于互通的可信局域网；屏幕已连接 Wi-Fi |

集成版本与屏幕固件版本独立编号。本仓库发布的是 **HA 集成 0.9.3**，不包含屏幕固件、设备密钥、配对文件或个人 HA 配置。

## 添加屏幕并选择内容

未绑定屏幕连接 Wi-Fi 后，HA 会通过局域网自动发现它。固件 2.3.1 及以上会持续等待首次添加，重启后也可添加。

1. 在「设置 → 设备与服务」找到 AD35 发现卡片，点击配置。没有发现卡片时，选择「添加集成 → AD35 HA Panel → 自动添加」，填写屏幕当前 IP 和 MAC。
2. 核对 HA 地址。这个地址必须能被浏览器和屏幕访问，不能使用 `localhost`、容器内部地址或带 `/lovelace` 的页面地址。
3. 按提示登录 HA 或授权，返回配置窗口，选择显示项目和天气，确认保存。

自动添加全程在 HA 操作，无需在屏幕点击允许。它需要固件 2.3.0 及以上，使用 HTTPS 8443，并核对设备证书与 MAC；2.3.0 的首次添加仍有五分钟窗口，建议先升级固件。临时码方式需要固件 2.2.0 及以上；配对文件或手动输入方式需要 2.1.2 及以上。配对文件由设备提供方单独提供。

已绑定的屏幕使用原条目的「⋮ → 重新配置」。仅为升级集成或修改显示内容，**无需删除条目、解绑或重新添加设备**。

### 可以显示哪些设备

最多选择 **64 个显示项目，另选 1 个天气实体**。控制项取决于对应 HA 实体实际支持的能力。

| 类型 | 屏幕上的功能 |
| --- | --- |
| 灯光 | 开关、亮度、颜色、色温 |
| 开关 | 开关控制 |
| 窗帘 | 开合、停止及实体支持的位置控制 |
| 空调 | 模式、温度、风速、摆风 |
| 传感器、状态传感器 | 状态显示 |
| 门锁 | 上锁、解锁；解锁时在屏幕再次确认，需要密码的锁请在 HA 操作 |
| 摄像头 | 点击查看静态画面，支持手动刷新，不提供实时视频 |
| 扫地机器人 | 实体支持的清扫控制 |
| 天气 | 首页天气显示 |

**显示项目和天气都可留空**，需要固件 2.7.2 及以上。固件 2.8.1 的空列表首页显示时钟和日期；选择天气后可同时显示天气。

「首页布局 · 位置 1–6」用于排列已选项目，不能重复；未指定的位置保留“自动补齐”。这些设置用于排序，不用于强制保留空白格。固件 2.8.1 会按已选数量调整首页布局。

## 更新集成与屏幕固件

### 更新 HA 集成

在 HACS 中下载新版本，然后重启 HA。手动安装的用户先备份原 `ad35_ha_panel` 文件夹，再用新版本替换并重启。**保留 HA 中现有 AD35 条目和屏幕绑定**。

0.9.3 增加 GitHub/HACS 发布所需的元数据、品牌资源和安装文档，运行逻辑与 0.9.2 一致。已有 0.9.2 的用户可直接升级。

### 更新屏幕固件

HACS 更新的是 HA 端集成，**不会替屏幕安装固件**。屏幕固件使用以下独立入口：

- **云端固件：** 打开 HA 中对应的 AD35 设备，点击「检查固件更新」，在「固件」卡片核对版本后点击安装。需要屏幕固件 2.4.0 或以上。更旧版本可先从屏幕「设置 → 固件升级」或原维护方式升级一次。
- **本地 `.bin`：** 打开「设置 → 设备与服务 → AD35 HA Panel → 对应设备的选项 → 本地固件升级」，上传这台屏幕专用的 OTA `.bin`，核对版本和摘要后确认。需要固件 2.5.0 或以上，不接受 `factory.bin`、其他设备的文件或降级文件。

安装过程中保持供电和网络，屏幕会重启。固件升级会保留 HA 绑定与已选内容。若提示结果尚未确认，先核对屏幕当前版本，再决定是否重试。正常使用时，集成不会自动安装屏幕固件。

## 常见问题

**安装后搜不到集成**  
先重启 HA，并刷新浏览器。手动安装时检查 `manifest.json` 的目录层级。

**未自动发现屏幕**  
确认屏幕已连接 Wi-Fi，HA 与屏幕可以互相访问；跨 VLAN 时需正确配置局域网发现和访问规则。也可使用「自动添加」填写屏幕 IP 和 MAC。

**在屏幕解绑后，没有出现新的发现卡片**  
HA 可能仍保留原条目。自动添加的条目通常约 60 秒内会出现重新认证提示；也可打开原条目的「⋮ → 重新配置 → 重新连接」。重新授权并保存即可，无需删除原设备及历史。

**保存过程中中断**  
使用原条目的重新配置，或「恢复已保存的配置」核对实际结果，避免重复提交。

**正常使用一段时间后出现 HA 授权失败**  
先检查屏幕实际固件是否已升级到 2.8.1。授权续期修复位于固件；只更新 HA 集成不会升级屏幕固件。

仍有问题可在 [Issues](https://github.com/zemismart-dev/ad35-ha-panel/issues) 提交集成版本、HA 版本、固件版本和复现步骤。分享日志前移除令牌、配对文件内容和设备密钥。
