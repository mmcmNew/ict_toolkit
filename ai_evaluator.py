"""
Модуль ИИ-оценки сделок (Google Gemini API / Fallback).

Оценивает качество сетапа перед входом:
- Согласованность с HTF bias (BOS/CHoCH)
- Качество liquidity sweep (хвост свечи, резкость)
- Характеристики FVG (величина зоны, свежесть)
- Наличие Order Block в зоне
- Нахождение внутри killzone и соотношение Risk/Reward

Возвращает структурированный ответ:
- score: 1-10
- recommendation: TAKE | CAUTION | SKIP
- confluence_strengths: список подтверждающих факторов
- risk_factors: список рисков/предупреждений
- reasoning: текстовое обоснование
"""
import os
import json
import config as cfg


def _rule_based_evaluation(setup: dict) -> dict:
    """
    Резервная количественная оценка на случай отсутствия GEMINI_API_KEY
    или проблем с сетевым доступом.
    """
    score = 5
    strengths = []
    risks = []

    # 1. Проверка направления и HTF bias
    expected_dir = setup.get("expected_dir", 0)
    bias = setup.get("bias", expected_dir)
    if expected_dir != 0 and expected_dir == bias:
        score += 2
        strengths.append(f"Идеальное совпадение с HTF bias ({'BULLISH' if bias==1 else 'BEARISH'})")
    else:
        score -= 2
        risks.append("Несоответствие или слабость старшего тренда (HTF bias)")

    # 2. Killzone
    is_kz = setup.get("in_killzone", False)
    if is_kz:
        score += 1
        strengths.append("Сетап сформирован строго внутри активной торговой сессии (Killzone)")
    else:
        risks.append("Сетап вне основных торговых сессий (азиатский флэт/тонкий рынок)")

    # 3. Размер FVG
    zone_pct = setup.get("zone_pct", 0.0)
    if 0 < zone_pct <= 0.003:
        score += 1
        strengths.append(f"Узкий, высокоточный FVG ({zone_pct*100:.2f}%)")
    elif zone_pct > 0.005:
        score -= 1
        risks.append(f"Широкая FVG-зона ({zone_pct*100:.2f}%) — повышенный риск проскальзывания и глубокого отката")

    # 4. Наличие Order Block
    if setup.get("has_ob", False):
        score += 1
        strengths.append("Дополнительное слияние (confluence) с Order Block")

    # 5. Ликвидность Азиатской сессии (Asian Range)
    if setup.get("is_asian_sweep", False):
        score += 2
        strengths.append(f"Классический Judas-свип ликвидности Азиатской сессии ({setup.get('asian_type', 'ASIAN')})")

    # 6. SMT-дивергенция (Smart Money Tool)
    if setup.get("has_smt", False):
        score += 2
        strengths.append("Институциональное подтверждение SMT-дивергенцией между BTC и ETH")

    # 7. Дистанция риска
    risk_pct = setup.get("risk_pct", 0.0)
    if risk_pct >= 0.004:
        score += 1
        strengths.append(f"Адекватная дистанция до стопа ({risk_pct*100:.2f}%) — защита от случайного рыночного шума")
    elif 0 < risk_pct < 0.0025:
        risks.append(f"Слишком узкий стоп ({risk_pct*100:.2f}%) — уязвим для комиссий и микро-спреда")

    # Ограничение 1-10
    score = max(1, min(10, score))

    if score >= 7:
        recommendation = "TAKE"
    elif score >= 5:
        recommendation = "CAUTION"
    else:
        recommendation = "SKIP"

    reasoning = (
        f"[Rule-based Fallback] Оценка сетапа {score}/10. "
        f"{'Рекомендуется к исполнению.' if recommendation=='TAKE' else 'Повышенный риск или слабый контекст.'} "
        f"Для включения анализа через нейросеть Gemini экспортируй GEMINI_API_KEY."
    )

    return {
        "score": score,
        "recommendation": recommendation,
        "confluence_strengths": strengths,
        "risk_factors": risks,
        "reasoning": reasoning,
        "source": "rule_based_fallback"
    }


def evaluate_setup(setup: dict, market_context: dict = None) -> dict:
    """
    Основная функция оценки сетапа. Использует Google Gemini API (через google-genai),
    а при отсутствии ключа — надежный quantitative fallback.
    """
    api_key = getattr(cfg, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    model_name = getattr(cfg, "GEMINI_MODEL", "gemini-2.5-flash")

    if not api_key:
        return _rule_based_evaluation(setup)

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        prompt = f"""
Ты — профессиональный количественный трейдер и эксперт по Smart Money Concepts (ICT).
Оцени предложенный сетап на вход в сделку.

Данные сетапа:
- Инструмент: {setup.get('symbol', 'UNKNOWN')}
- Направление: {'LONG' if setup.get('expected_dir') == 1 else 'SHORT'}
- Время свипа ликвидности: {setup.get('sweep_time')}
- Время подтверждения (FVG): {setup.get('confirm_time')}
- HTF Bias (1H): {'BULLISH (1)' if setup.get('bias') == 1 else 'BEARISH (-1)' if setup.get('bias') == -1 else 'NEUTRAL'}
- Внутри Killzone: {setup.get('in_killzone', False)}
- Свип экстремума Азиатской сессии: {setup.get('is_asian_sweep', False)} ({setup.get('asian_type', 'N/A')})
- Наличие SMT-дивергенции (BTC vs ETH): {setup.get('has_smt', False)}
- Ширина FVG зоны: {setup.get('zone_pct', 0.0)*100:.3f}% (Top: {setup.get('fvg_top')}, Bottom: {setup.get('fvg_bottom')})
- Наличие Order Block: {setup.get('has_ob', False)}
- Ожидаемый риск: {setup.get('risk_pct', 0.0)*100:.3f}%
- Целевое соотношение: 2R частичный тейк + трейлинг

Дополнительный контекст рынка:
{json.dumps(market_context or {}, ensure_ascii=False, indent=2)}

Требования к ответу:
Верни строго JSON со следующей структурой:
{{
  "score": <число от 1 до 10, где 10 — эталонный сетап высокой вероятности>,
  "recommendation": <"TAKE" | "CAUTION" | "SKIP">,
  "confluence_strengths": [<список сильных сторон и факторов совпадения>],
  "risk_factors": [<список потенциальных опасностей и красных флагов>],
  "reasoning": <краткое, четкое обоснование решения в 2-3 предложениях>
}}
"""
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            )
        )

        result = json.loads(response.text)
        result["source"] = f"gemini_{model_name}"
        return result

    except Exception as e:
        fallback = _rule_based_evaluation(setup)
        fallback["reasoning"] += f" (Ошибка вызова Gemini API: {e})"
        return fallback
