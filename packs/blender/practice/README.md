# Blender 历史 PR 练习数据

首批 10 个案例均从本地完整 Blender Git 历史手工筛选，并逐个核对修复提交正文和 diff；没有进行全网批量抓取。

- `catalog.json`：练习阶段可见。包含问题现象、复现轮廓、修复前 commit、建议时间和 AI 运行预算。
- `answers.json`：仅在用户提交根因判断并选择揭晓后读取。包含真实修复 commit、PR 编号、根因、相关文件/符号、关键证据、验证实验和常见误区。
- `rubric.json`：记录五项质量评分和耗时、AI 运行次数两项资源指标。

未来的练习模式必须把两个文件视为不同权限层：开始练习、创建 Case 和 Codex 调查时不得将 `answers.json` 放入上下文。答案比较应记录用户用时和 AI 运行次数，但不修改原始案例数据。

案例的 `pre_fix_commit` 是修复提交的第一父提交。数据来源是 `/Users/ayanami/blender-git/blender` 中可解析的 Git 对象；Pack 不依赖这个绝对路径。
