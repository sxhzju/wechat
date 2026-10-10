# 微信公众号草稿工具

一个入口，使用 Python 标准库，无需安装依赖。
本机通过 SSH 隧道调用微信官方 API，固定出口为腾讯云轻量服务器 `YOUR_SERVER_PUBLIC_IP`。

## 日常操作

在项目根目录执行：

```sh
# 检查本地配置，不联网
python3 wechat.py check

# 一次检查 SSH、微信认证和草稿箱权限
python3 wechat.py auth

# 上传正文图片与封面，保存到草稿箱并读取验证
python3 wechat.py draft --article path/to/article.json
```

工具只保存草稿。相同内容再次运行时复用最近一次记录的草稿 ID 并读取验证。
若微信已经接收请求，但创建响应丢失，需先在后台核对草稿，避免重复提交。

## 配置

`.env` 保存 AppID、AppSecret 和 SSH 配置，文件权限为 `600`，不提交到 Git。
变量说明见 `.env.example`；需要更新公众号凭据时运行：

```sh
python3 wechat.py configure
```

该命令隐藏 AppSecret 输入，并保留 SSH 出口设置。环境变量可覆盖文件配置。

SSH 配置示例（已脱敏，需按自己的环境配置）：

- `~/.ssh/YOUR_SSH_PRIVATE_KEY`：本机私钥。
- `~/.ssh/YOUR_SSH_KNOWN_HOSTS`：经腾讯云控制台核验的服务器主机公钥。
- 服务器 `YOUR_SSH_USER` 账户需授权对应公钥，限制本地转发目标为 `api.weixin.qq.com:443`，禁止运行 shell。
- 微信 API IP 白名单需添加 `YOUR_SERVER_PUBLIC_IP`。

每次联网命令自动建立、使用并关闭隧道；只监听本机回环地址，严格校验 SSH 主机公钥和微信 TLS 证书。
连接失败直接报错。AppSecret 保留在本机，token 只保存在进程内存中。
更换 Mac 时需要授权新设备公钥；更换服务器公网 IP 时需要同步 SSH 配置及微信 IP 白名单。

## 文章格式

```json
{
  "title": "文章标题",
  "digest": "文章摘要",
  "content_file": "article.html",
  "cover_file": "cover.jpg",
  "author": "",
  "need_open_comment": 1,
  "only_fans_can_comment": 0
}
```

文件路径相对于文章 JSON。`need_open_comment` 省略时默认 `1`（开启留言）。
赞赏无法通过接口设置（草稿与发布接口均无对应字段），需在后台发表时手动勾选。
HTML 中的本地图片写成 `<img src="asset://images/figure.png">`，
上传后自动替换为微信图片 URL。JPG/PNG 走 `media/uploadimg`（≤1MB）；
GIF 走永久图片素材 `material/add_material`（≤10MB，占素材库名额）。
成功后，`draft-result.json` 保存草稿 ID 和验证结果，不含密钥或 token。

本仓库已脱敏，历史验证结果不代表占位配置可用；填写自己的配置后重新执行本地检查及认证。

## 脱敏占位符说明

本目录为脱敏副本，原目录未修改。`.env` 和 `.env.example` 中的每项均有注释。

- `YOUR_WECHAT_APP_ID` / `YOUR_WECHAT_APP_SECRET`：公众号标识及密钥。
- `YOUR_SSH_USER` / `YOUR_SSH_HOST`：SSH 登录用户名及服务器地址。
- `YOUR_SERVER_PUBLIC_IP`：服务器公网出口 IP，用于配置微信 API 白名单。
- `~/.ssh/YOUR_SSH_PRIVATE_KEY` / `~/.ssh/YOUR_SSH_KNOWN_HOSTS`：本机 SSH 私钥与主机公钥记录文件路径；不包含这些文件本身。
- `draft-result.json` 中的 `YOUR_DRAFT_MEDIA_ID`、`YOUR_ARTICLE_TITLE`、`YOUR_ARTICLE_FINGERPRINT`：草稿标识、文章标题、内容指纹。JSON 不支持注释，说明放在 `_comment` 字段中；验证状态已重置。

已删除复制目录中的 `__pycache__` 编译缓存，避免缓存留存历史配置或路径；Python 源码保留。
