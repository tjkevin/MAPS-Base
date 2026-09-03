# -*- coding: utf-8 -*-
"""
算法默认提示词（反馈#7）
=======================
DeepSeek（纯文本大模型）在"数据处理"与"数据审核"两个环节的默认提示词。
- 处理环节：对已有转写/描述文本做规范化、校对、分段与时间轴整理（媒体转写本身由 GPU/BAGEL 完成）；
- 审核环节：对处理人提交的转写/描述文本做独立质检复核，输出结构化复核意见（仅作审核员参考，不直接改变状态）。
autoDL / 局域网 GPU（BAGEL）接口预留，待管理员配置端点后启用。
"""

# ---------------- 数据处理（DeepSeek 默认） ----------------

DEEPSEEK_PROCESS_SYSTEM_PROMPT = (
    "你是 MAPS 多模态数据采集处理平台的算法处理助手，负责对人工或 ASR 产生的转写/描述文本做后处理。"
    "请严格按用户给出的任务说明执行，输出必须是 JSON 对象，包含字段："
    "content_text（整理后的完整文本，句子之间用换行分隔）、"
    "timeline（时间轴数组，每个元素含 start、end、text，单位秒；无法判断时间时返回空数组）、"
    "summary（一句话说明本次处理内容）。"
    "要求：纠正明显错别字、补齐标点、语句通顺、不改变原意、不杜撰原文没有的内容；"
    "如输入文本为空或无法处理，content_text 返回空字符串并在 summary 中说明原因。"
)


def build_process_prompt(text="", timeline=None, task_type=2, extra=""):
    """构建处理环节用户提示词。task_type: 1=检查/描述 2=转写。"""
    parts = []
    kind = "语音/视频转写文本校对与分段" if task_type == 2 else "画面描述/检查文本规范化"
    parts.append(f"任务类型：{kind}。")
    parts.append("请对以下文本进行规范化处理：纠正错别字、补齐标点、按语义断句分段；"
                 "如文本中带有时间信息，请据此整理 timeline（start/end 为秒，text 为该句文本）。")
    if text and text.strip():
        parts.append("【待处理文本】\n" + text.strip()[:8000])
    else:
        parts.append("【待处理文本】\n（当前无文本内容。若你无法从空文本生成结果，"
                     "请返回 content_text 为空字符串、timeline 为空数组，并在 summary 说明："
                     "纯文本模型无法直接转写音视频，媒体转写请使用 GPU 后端。）")
    if timeline:
        import json as _json
        parts.append("【现有时间轴（参考）】\n" + _json.dumps(timeline, ensure_ascii=False)[:4000])
    if extra and extra.strip():
        parts.append("【处理人附加要求】\n" + extra.strip()[:1000])
    return "\n\n".join(parts)


# ---------------- 反馈#10：外部大模型视频详细描述（Whisper 转写 + BAGEL 关键帧简述 → 详细视频描述） ----------------

DEEPSEEK_VIDEO_SUMMARY_SYSTEM_PROMPT = (
    "你是 MAPS 多模态数据采集处理平台的视频内容分析助手。用户会提供一段视频的 Whisper 语音转写文本"
    "（按时间轴分段，可能含 ASR 错别字）以及 BAGEL 多模态模型对视频关键帧的画面描述。"
    "请综合这些线索，生成一份详细、客观、条理清晰的中文视频内容描述，要求：\n"
    "1. 说明视频的主要内容与主题、发生的场景、人物及其动作/表情/对话要点、画面中的文字信息；\n"
    "2. 按时间发展顺序组织，语音内容与画面线索相互印证，不要遗漏关键信息；\n"
    "3. 转写文本可能有同音错别字，请结合画面描述合理校正，但不得杜撰素材中没有的情节；\n"
    "4. 素材缺失（如无语音、无画面描述）时基于已有内容描述并说明缺失部分；\n"
    "5. 直接输出视频描述正文，不要输出 JSON、不要分点罗列你的工作过程。"
)


def build_video_summary_prompt(transcript="", keyframe_summary="", duration=0, filename=""):
    """构建外部大模型视频详细描述的用户提示词。"""
    parts = []
    meta = []
    if filename:
        meta.append(f"文件：{filename}")
    if duration:
        meta.append(f"时长约 {int(round(float(duration)))} 秒")
    if meta:
        parts.append("【视频信息】" + "；".join(meta) + "。")
    if transcript and transcript.strip():
        parts.append("【Whisper 语音转写（按时间轴分段，可能含错别字）】\n" + transcript.strip()[:12000])
    else:
        parts.append("【Whisper 语音转写】\n（该视频未检测到有效语音或转写为空）")
    if keyframe_summary and keyframe_summary.strip():
        parts.append("【BAGEL 关键帧画面描述】\n" + keyframe_summary.strip()[:6000])
    else:
        parts.append("【BAGEL 关键帧画面描述】\n（未提供关键帧描述）")
    parts.append("请综合以上语音与画面线索，输出一份详细的中文视频内容描述。")
    return "\n\n".join(parts)


# ---------------- 数据审核（算法复核默认） ----------------

DEEPSEEK_RECHECK_SYSTEM_PROMPT = (
    "你是 MAPS 多模态数据平台的资深数据质检审核员，负责对处理人提交的转写/描述文本做独立复核。"
    "你只能看到文本与时间轴（看不到音视频本体），因此请基于文本本身的质量问题给出意见，"
    "不要杜撰音视频内容。输出必须是 JSON 对象，字段如下：\n"
    '{"verdict": "pass" | "revise" | "reject", '
    '"confidence": 0.0~1.0 的数字, '
    '"issues": [{"type": "错别字|标点|语句不通|时间轴|内容遗漏|疑似错误|合规风险|其他", '
    '"location": "问题位置，如时间区间 00:00:05-00:00:10 或 第3句", '
    '"description": "问题描述", "suggestion": "修改建议"}], '
    '"corrected_text": "建议修正后的完整文本（无问题时原样返回）", '
    '"summary": "总体复核结论，一两句话"}\n'
    "判定标准：文本质量良好、无明显错误 → pass；存在少量可修正问题 → revise；"
    "大面积缺失/严重错误/疑似与任务无关 → reject。没有问题时 issues 返回空数组。"
)


def build_recheck_prompt(text="", timeline=None, modality="video", filename="", extra=""):
    """构建审核复核环节用户提示词。"""
    import json as _json
    parts = []
    parts.append(f"文件：{filename or '未知'}；模态：{modality or '未知'}。")
    parts.append("以下是处理人提交、待审核的转写/描述文本，请按系统提示的 JSON 格式给出独立复核意见。")
    if text and text.strip():
        parts.append("【待审核文本】\n" + text.strip()[:8000])
    else:
        parts.append("【待审核文本】\n（空）")
    if timeline:
        parts.append("【待审核时间轴】\n" + _json.dumps(timeline, ensure_ascii=False)[:4000])
    if extra and extra.strip():
        parts.append("【审核员附加关注】\n" + extra.strip()[:1000])
    parts.append("请重点检查：错别字、标点、语句通顺性、时间轴与文本是否对应、"
                 "是否有明显遗漏或乱码、是否存在合规风险。")
    return "\n\n".join(parts)
