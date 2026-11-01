"""Versioned task definitions. A batch copies these values before any request."""
from copy import deepcopy

DISCLAIMER = "本项目视觉任务评分，不是官方基准分或 IQ；不能代表总体智力，也不能证明接口上游模型身份。"
RUBRIC = {
    "version": "1.0", "name": "本项目鹈鹕视觉评分", "maximum": 20,
    "dimensions": [
        {"id": "A", "title": "鹈鹕辨识", "criteria": "观察长喙、喉囊、头颈和身体是否形成可辨识的鹈鹕；不能因写着 pelican 就给高分。"},
        {"id": "B", "title": "自行车结构", "criteria": "观察双轮、车架、前叉、车把、车座和踏板是否合理连接；两个圆不等于完整自行车。"},
        {"id": "C", "title": "骑乘关系", "criteria": "身体与车座有合理支撑，脚接近或接触踏板，翅膀或拟人化前肢合理接近车把；鸟与车形成骑乘关系；检查悬空、穿插、连接错误及简单叠放。"},
        {"id": "D", "title": "构图与可读性", "criteria": "观察画面完整性、主体裁切、遮挡和结构可读性；装饰、配色或文字解释不直接加分。"},
    ],
    "scale": ["缺失或无法辨识", "有明显意图，但存在重大错误", "部分成立，仍有严重缺陷", "基本清晰，存在多处问题", "整体良好，只有少量问题", "表现完整、清楚，无明显相关缺陷"],
    "disagreement": {"total": 4, "dimension": 2},
    "notice": DISCLAIMER,
}

# New tasks belong here, with their own verified prompt and matching rubric.
# Do not apply the visual rubric to mathematics or logic tasks.
QUESTIONS = [{
    "id": "pelican-bicycle", "version": "1.0", "title": "鹈鹕骑自行车",
    "category": "视觉结构与指令执行", "abilityTags": ["指令理解", "SVG 生成", "空间结构", "骑乘关系", "重复稳定性"],
    "prompt": "Generate an SVG of a pelican riding a bicycle", "outputFormat": "SVG",
    "source": "https://github.com/simonw/pelican-bicycle",
    "scoringRubric": RUBRIC, "rubricVersion": "1.0", "enabled": True,
    "mode": "original-baseline", "official": False,
    "baseline": "原题逐字发送；仅一条用户消息，无系统消息、工具、上下文、修图或自动重试。预览第一个完整 SVG，原始回答完整保留（密钥脱敏除外）。",
}]


def catalog():
    return deepcopy(QUESTIONS)


def question_snapshot(ids):
    lookup = {q["id"]: q for q in QUESTIONS if q["enabled"]}
    if not isinstance(ids, list) or not ids or len(ids) > 30 or any(i not in lookup for i in ids):
        raise ValueError("请选择已启用的题目")
    return [deepcopy(lookup[i]) for i in dict.fromkeys(ids)]
