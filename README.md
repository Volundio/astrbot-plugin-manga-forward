# astrbot-plugin-manga-forward（漫画整话转发）

一款严格遵循 [AstrBot 插件开发规范](https://docs.astrbot.app/dev/star/plugin-new.html) 的漫画整话跨源解析与合并转发插件。通过 `manga` 指令即可按自定义优先级从 **再漫画**、**拷贝漫画**、**Komiic** 检索并下载整话高清图片，经 **OneBot v11 (`aiocqhttp`)** 合并转发（**一张图片一条消息，严格保证漫画页码顺序**）发送给用户，并支持在最后附上单话评论 / 章末吐槽。

---

## ✨ 功能特性

- **多漫画源自由调整优先级**：内置支持 **再漫画 (`zaimanhua`)**、**拷贝漫画 (`copymanga`)**、**Komiic (`komiic`)**，可在 AstrBot WebUI 管理面板自由调整先后顺序与独立开关；高优先级源未收录、缺话或受限时自动平滑降级到下一源。
- **再漫画离线索引检索**：严格通过第三方静态索引站 [`zmh.shadows.de5.net`](https://zmh.shadows.de5.net/?needLogin=only1) 检索具体漫画 ID（支持需登录隐藏漫画与公开漫画，本地 SHA256 分片缓存毫秒级响应，**绝不调用官方搜索接口**）。
- **双源登录态持久化与失效自动重登**：
  - **再漫画**：支持配置账号密码登录（自动保存 Cookie/Token 至插件数据目录，过期或鉴权失效时自动重登刷新，可选开启每日自动签到）或直接配置浏览器 Cookie / Bearer Token。
  - **Komiic**：支持配置邮箱账号密码登录（自动保存会话 Cookie，临近过期优先 `/auth/refresh` 续期、失败自动重登，解除未登录免费读图上限）或直接配置 Cookie。
- **严格保证整话图片顺序**：
  - 有界并发预下载（自动携带各源防盗链 `Referer` / `Cookie` / `Authorization`），按固定页码槽位归并，**一张图片独占一条合并转发节点消息**；
  - 完整还原拷贝漫画 `/chapter2/` 接口 `words` 打乱页序映射，支持 Web 端 AES-128-CBC 解密双栈容灾；
  - 超多页或大体积章节自动按序分批合并转发，发送完成后立即清理本地临时文件，零磁盘残留。
- **单话评论 / 章末吐槽**：当推送源为**再漫画**或**拷贝漫画**时，支持在整话合并转发末尾自动附上该话的热门单话评论 / 章末吐槽。
- **智能指令与简繁互通**：内置简繁双向归一化、中文数字（如 `第一百二十话`）、小数话（`12.5话`）、含空格漫画名、单行本卷/连载话隔离与 `最新话` 快捷定位。

---

## 📦 适用平台与环境要求

- **AstrBot 版本**：`>= 4.10.0`
- **支持平台适配器**：一期仅支持 **OneBot v11 (`aiocqhttp`)**（NapCat / Lagrange / LLOneBot 等协议端）

### 安装方式

在 AstrBot 插件市场搜索安装，或手动克隆至 `AstrBot/data/plugins/` 目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/Volundio/astrbot-plugin-manga-forward
```

随后在 AstrBot WebUI **扩展 → 插件** 页面重载或启用插件，按需配置账号凭据与漫画源优先级即可。

---

## 📖 指令用法

通过 `manga` 关键字触发（若 AstrBot 系统唤醒词为 `/`，则使用 `/manga`）：

```text
/manga <漫画名> <第几话>
```

### 常用示例

| 指令示例 | 说明 |
| --- | --- |
| `/manga 电锯人 第1话` | 按配置的源优先级检索《电锯人》第 1 话并整话合并转发 |
| `/manga 葬送的芙莉莲 120` | 支持直接输入纯数字话数或中文数字（如 `第一百二十话`） |
| `/manga 电锯人 12.5话` | 支持小数番外/附篇话数精准匹配 |
| `/manga 孤独摇滚 第1卷` | 显式含“卷”时匹配单行本整卷（与“第1话”严格区分） |
| `/manga 药屋少女的呢喃 最新话` | 自动定位并推送当前连载最新一话 |
| `/manga 《电锯人 第二部》 135` | 漫画名含空格时可直接输入或用引号/书名号包裹 |
| `/manga 电锯人 1 -s komiic` | 可选通过 `-s` / `--source`（`再漫画` / `拷贝` / `komiic`）临时指定单次漫画源 |
| `/manga zmh:57069 第1话` | 支持通过 `zmh:<id>`、`copy:<path_word>`、`komiic:<id>` 直达指定漫画 ID |

---

## ⚙️ 插件配置说明

所有配置均可在 AstrBot WebUI 插件管理面板可视化调整，持久化数据统一存放在 `data/plugin_data/astrbot_plugin_manga_forward/`：

- **`source_priority`**：漫画源先后优先级列表（默认 `["zaimanhua", "copymanga", "komiic"]`）。
- **`zaimanhua`（再漫画配置）**：
  - `enabled`：是否启用再漫画源。
  - `search_site_url`：第三方静态索引站点地址（默认 `https://zmh.shadows.de5.net/?needLogin=only1`）。
  - `need_login_filter`：索引筛选范围（`any` 全部漫画、`only1` 仅需登录隐藏漫画、`only0` 仅公开漫画）。
  - `username` / `password`：再漫画账号与密码（自动登录保存 Cookie/Token，失效后自动重登刷新）。
  - `cookie` / `prefer_cookie`：直接配置再漫画 Cookie 或 Bearer Token。
  - `auto_sign_in`：登录态有效时每日自动执行一次再漫画签到。
- **`copymanga`（拷贝漫画配置）**：
  - `enabled`、`api_domain`、`web_domain`、`image_resolution`（`800` / `1200` / `1500`）、可选 `token`。
- **`komiic`（Komiic 配置）**：
  - `enabled`、`base_url`、`username`（邮箱） / `password`、`cookie`、`prefer_cookie`。
- **`comments`（单话评论 / 章末吐槽）**：
  - `enable_comments`：推送再漫画或拷贝漫画源时是否在末尾附上章末吐槽（默认开启）。
  - `max_comments`：最多附带的吐槽条数（默认 `20`）。
- **`delivery` & `network`**：合并转发首条概览开关、单批最大页数与体积上限、解析等待提示与自动撤回、HTTP 代理及并发连接数。

---

## 🙏 致谢与参考项目

本插件在接口协议梳理与合并转发设计过程中参考了以下优秀的开源项目与站点，特此致谢：

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [再漫画第三方搜索索引站 (`zmh.shadows.de5.net`)](https://zmh.shadows.de5.net/?needLogin=only1)
- [DIYgod/RSSHub — 再漫画路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/zaimanhua/comic.ts)、[拷贝漫画路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/copymanga/comic.tsx)、[Komiic 路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/komiic/comic.ts)
- [keiyoushi/extensions-source — Mihon 再漫画扩展](https://github.com/keiyoushi/extensions-source/tree/main/src/zh/zaimanhua/src/eu/kanade/tachiyomi/extension/zh/zaimanhua) 与 [Komiic 扩展](https://github.com/keiyoushi/extensions-source/tree/main/src/zh/komiic/src/eu/kanade/tachiyomi/extension/zh/komiic)
- [LittleSurvival/copymanga-copy20 — Mihon 拷贝漫画扩展](https://github.com/LittleSurvival/copymanga-copy20)
- [qd-today/templates — 再漫画签到模板](https://github.com/qd-today/templates/blob/master/%E5%86%8D%E6%BC%AB%E7%94%BB.har)
- [FlanChanXwO/astrbot_plugin_rsshub — AstrBot RSSHub 推送插件](https://github.com/FlanChanXwO/astrbot_plugin_rsshub)
