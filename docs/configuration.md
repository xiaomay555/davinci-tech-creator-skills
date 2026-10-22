# 本地配置

所有路径由使用者指定，仓库不包含原作者的磁盘、网络地址、账号和素材。

- `AI_EDIT_DATA_DIR`：B-roll 学习库和花字案例库的数据根目录，默认使用当前用户数据目录。
- `AI_EDIT_SFX_DIR`：自备音效库。`engine/config/sfx_favorites.json` 是文件名与选段示例，需按自己实际持有的素材修改。未配置时使用仓库内相对目录 `assets/sfx`，该目录不随仓库提供素材。
- `AI_EDIT_FONT_DIR`：准备脚本读取字体的目录，默认相对目录 `assets/fonts`。请自行取得字体并检查许可。
- `CODEX_HOME`：可选，沿用当前 Codex 配置目录。

B-roll 还可通过用户主目录内 `.config/ai-talking-head-edit/broll.json` 覆盖 `nas_mount`、`ledger_index`、`learning_root`、`creator` 与 `allow_postgres_apply`。三个数据路径应指向同一数据根目录内的文件，数据根目录须事先存在，不能放进代码仓库；默认禁止 PostgreSQL 写入，协作数据库需另行验证和明确开启。

项目媒体、字幕、审核状态和交付物留在各项目目录；首次使用时确认项目路径、LUT、时间线画幅和帧率。公开示例中的字体与样式只是可选起点，不能覆盖使用者的手改或偏好。
