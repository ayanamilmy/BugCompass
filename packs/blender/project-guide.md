# Blender 调查起点

这是 BugCompass 0.1 的初始知识包，只用于缩小搜索范围。它不完整，也不能替代源码、Git 历史和可复现证据；应随真实案件逐步补充。

## 基础区域

- `source/blender/depsgraph`：依赖图建立、标记与求值。
- `source/blender/editors`：编辑器、操作符、事件和用户交互。
- `source/blender/blenkernel`：核心数据处理、ID 数据块和生命周期。
- `source/blender/makesrna`、`source/blender/makesdna`：RNA 属性、更新回调和数据结构。
- `source/blender/nodes/geometry`、`source/blender/geometry`：Geometry Nodes 与几何算法。
- `source/blender/render`、`intern/cycles`：渲染与 Cycles。
- `source/blender/editors/interface`：UI 控件、布局和状态。

## 常见调查维度

- 数据更新：写入发生在哪里，更新标签是否沿依赖传播。
- 通知事件：操作完成后是否发送正确 notifier，哪些编辑器消费它。
- 缓存失效：缓存的键、所有者和失效条件是否覆盖当前操作。
- Undo：状态是否被正确记录、恢复，重做路径是否等价。
- 所有权：指针、ID 数据块和临时对象由谁持有，跨层传递是否安全。
- 生命周期：创建、复制、释放和文件加载阶段是否存在时序差异。

这些维度是搜索提示，不应在缺少源码证据时被写成结论。
