"""Task capabilities and input contracts shared by API, UI and execution."""

CATALOG = {
    "extraction": ("知识抽取", "在选定文档范围抽取实体、关系和证据", ["reextract_documents"]),
    "ingestion": ("资料接入", "评估文档质量、元数据并安排批量索引或登记连接器同步", ["reindex_documents", "sync_sources", "tag_documents"]),
    "schema_governance": ("Schema 治理", "对比领域类型，生成可审阅草稿和重建影响", ["create_schema_draft"]),
    "quality_review": ("质量复核", "发现弱证据、冲突、重复实体并形成有证据的修复候选", ["propose_correction", "apply_correction"]),
    "incremental_maintenance": ("增量维护", "对比文档版本，只重新处理变化的文档和图谱", ["reextract_documents", "reindex_documents"]),
    "knowledge_organization": ("知识组织", "整理标签、主题社区并形成引用原文的专题包", ["tag_documents", "rebuild_communities"]),
    "evaluation": ("检索评测", "以金标准评估检索命中、召回、耗时和代理用量", ["evaluate_cases"]),
    "business_analysis": ("业务分析", "生成有出处的依赖、风险、时间线及业务分析报告", []),
    "operations": ("运行运维", "诊断任务和服务状态，有限重试、调整批次或选择已配置模型", ["retry_jobs", "adjust_batch", "select_model_profile"]),
}

TASK_INSTRUCTIONS = {
    "extraction": "逐段检查实体、指代、关系方向和遗漏。引文必须来自选定片段；用reextract_documents安排受控抽取，不把规划本身宣称为知识已入库。",
    "ingestion": "检查文档状态、空片段、重复内容与版本，提出接入或标签计划。只同步用户指定的登记源；使用现有索引服务，不提出未支持的切分参数或任意本地路径。",
    "schema_governance": "全知识库共用一个Schema，对比当前Schema与原文领域概念，给出类型增删、命名和关系约束理由，不按专业方向另建Schema。允许create_schema_draft时必须提出该动作，params包含非空entity_types/relation_types和规范化规则；无动作授权时仅输出草案建议与依据。仅建全库Schema版本草稿，不激活或修改生效Schema。",
    "quality_review": "检查弱证据、冲突、重复和关系方向，逐项引用原文。证据不足时列出待核验问题；优先propose_correction候选，不把猜测提升为高置信事实。",
    "incremental_maintenance": "根据document_versions、文件哈希与待处理状态选择变化范围，解释版本差异和预期影响。避免对未变化文档安排重复全量工作。",
    "knowledge_organization": "按业务概念整理标签与主题社区，为专题包给出标题、摘要和引用清单。保留已有标签；社区无实体时应无操作，禁止扩大到未选文档。",
    "evaluation": "使用用户金标准和登记模型/提示词对照。失败题必须保留并计入失败分母，区分检索指标、引用覆盖与语义正确性；未知总量和定价保持未知，不捏造分数或费用。",
    "business_analysis": "结合business_artifact中的有效依赖、影响路径、循环及原文日期分析业务风险。所有事实需引文，推断明确标注；未知日期、未证实依赖和范围外事实不能补成结论。",
    "operations": "先说明实际诊断证据，再提出有限修复。只能重试用户指定的失败任务、在1–100范围调整后续批次，或切换服务器登记模型；禁止shell、任意URL、密钥及无界重试。",
}


def task_catalog():
    from app.agents.factory import list_backend_profiles
    return [{"task": task, "label": value[0], "description": value[1], "instructions": TASK_INSTRUCTIONS[task], "allowed_actions": value[2],
             "options": {"apply": False, "max_actions": 10, "runtime_profiles": list_backend_profiles()}}
            for task, value in CATALOG.items()]
