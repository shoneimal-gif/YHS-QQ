# AlphaAgent Paper 跟单

当前链路为 personal observer → 持久化原始证据 → personal source → Paper copier。
用于自有 Discord 群的转发消息只是通知，不作为原频道来源认证的替代品。
代码保留历史模式兼容分支；本次开发与部署目标始终是 Dennis-only direct source。

来源身份、CREATE时间、修改/删除证据和校验策略必须一致，不能仅按消息文字触发订单。
`verified_embed_transport_v2` 区分内容变化与经过验证的传输字段变化。

入口：

- `personal_cloud_runner.py`：隔离信号服务及私有 systemd 凭据边界。
- `runner.py`：独立交易服务；配置授权通过后才加载 Paper 凭据。
- `diagnostics.py`：只读一致静态快照 CLI；不导入上述入口或接触网络。

示例字段见 `config.personal.paper.example.json`；示例故意不可执行。
详见根目录 [DEVELOPMENT.md](../DEVELOPMENT.md) 与 [AGENTS.md](../AGENTS.md)。
