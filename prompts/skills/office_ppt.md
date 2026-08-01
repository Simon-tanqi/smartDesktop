# PowerPoint 演示文稿生成

使用 `generate_ppt` 一次性生成视觉精美的 PPTX。模板共 8 种：cover / section / content / icon_grid / chart / table / image_text / comparison，具体参数见工具 docstring。

---

## 强制流程

```
搜索调研 → 规划大纲 → 用户确认 → 一次性生成 → 交付
```

### 1. 搜索调研
围绕主题搜索 1~2 轮（`web_search`），尝试收集最新数据、案例、趋势。若搜索结果不足或被过滤，**允许基于模型已有知识生成**，但在数据处标注"(基于模型知识，未实时验证)"。

### 2. 规划大纲
基于素材规划每页的模板类型和标题，输出大纲草稿给用户确认。结构参考：

```
封面(cover) → 目录(content) → 章节1(section+content×2+chart) → 
章节2(section+content+icon_grid) → 章节3(section+content+comparison) → 
总结(content)
```

### 3. 用户确认
展示总页数 + 每页模板/标题，用户确认后再生成。演讲/分享类 ≥15 页，汇报类 ≥10 页。

### 4. 一次性生成
构造完整 slides 数组，单次调用 `generate_ppt`。15 页约需 1.5-3 分钟，调用前告知用户。

---

## 硬性约束

- **搜索先行**：至少尝试 1 轮搜索，搜索不可用时基于模型知识生成并标注
- **大纲确认**：禁止跳过用户确认直接生成
- **禁止空白页**：每页 items/paragraphs/rows 不可为空
- **模板多样**：全篇至少用 3 种模板，禁止只用 cover+content
- **数据溯源**：图表/表格数据优先来自搜索素材，不可用时基于模型知识并标注
- **一次性提交**：禁止分多次调用 generate_ppt

## 主题配色

`tech_blue`(科技) / `business_gray`(商务) / `gradient_purple`(创意) / `education_orange`(教育) / `minimal_dark`(极简)
