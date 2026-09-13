"""
Генератор датасета для Supervised Fine-Tuning Google Gemini.
Преобразует исторические сделки бэктеста (с реальными исходами R) в формат JSONL
для дообучения моделей Gemini через Google AI Studio или Google Cloud Vertex AI.
"""
import os
import glob
import json
import pandas as pd

SYSTEM_INSTRUCTION = """Ты — ведущий квантовый аналитик и эксперт по институциональной методологии Smart Money Concepts (ICT).
Твоя задача — оценивать сетапы перед входом в сделку по 10-балльной шкале и отсеивать ловушки ликвидности.

Правила институционального анализа:
1. Запрещено торговать контртренд против макро-структуры D1 без подтвержденного слома структуры (MSS) на ключевом пуле ликвидности.
2. Сетап обязан формироваться внутри активной сессии (London 07:00-10:00 UTC, New York 12:00-15:00 UTC / 11:00-15:00 MSK).
3. Вход осуществляется на 50% Consequent Encroachment (CE) FVG для минимизации дистанции стопа.
4. Стоп-лосс меньше 0.25% уязвим для рыночного шума и комиссий — снижай оценку.
5. Свип азиатской ликвидности (Judas Swing) в направлении HTF Bias — сетап высшей вероятности (8-10 баллов).
"""


def build_tuning_dataset(reports_dir: str = "reports", output_file: str = "data/gemini_ict_tuning_dataset.jsonl") -> int:
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    csv_files = glob.glob(os.path.join(reports_dir, "backtest_results_*.csv"))
    
    samples = []
    for f in csv_files:
        df = pd.read_csv(f)
        if len(df) == 0 or "R" not in df.columns or "dir" not in df.columns:
            continue
        
        for _, row in df.iterrows():
            r_val = float(row["R"])
            outcome = str(row.get("outcome", ""))
            symbol = str(row.get("symbol", ""))
            direction = str(row.get("dir", ""))
            fill_price = float(row.get("fill_price", 0))
            stop = float(row.get("stop", 0))
            risk_pct = abs(fill_price - stop) / fill_price if fill_price else 0.003
            is_asian = bool(row.get("is_asian_sweep", False))
            has_smt = bool(row.get("has_smt", False))

            # Определяем идеальный ответ для обучения
            if r_val >= 1.2 or "PARTIAL" in outcome:
                score = min(10, 8 + int(r_val >= 2.0))
                recommendation = "TAKE"
                strengths = ["Чистое исполнение в направлении ордерфлоу", "Импульсный выход из зоны дисбаланса"]
                if is_asian:
                    strengths.append("Подтвержденный Judas-свип азиатской ликвидности")
                if has_smt:
                    strengths.append("Институциональное подтверждение SMT-дивергенцией")
                risks = ["Стандартный рыночный риск"]
                reasoning = (
                    f"Сетап {symbol} {direction} обладает высоким институциональным слиянием факторов. "
                    f"Ожидается чистая экспансия с высоким R:R без угрозы первичного стопа."
                )
            elif outcome == "BE_STOP" or abs(r_val) < 0.2:
                score = 5
                recommendation = "CAUTION"
                strengths = ["Первичное движение в сторону сетапа"]
                risks = ["Сжатие волатильности, риск возврата в точку входа", "Отсутствие импульсного продолжения"]
                reasoning = (
                    f"Сетап {symbol} {direction} имеет ограниченный потенциал хода. "
                    f"Рекомендуется снизить объем или зафиксировать ранний частичный тейк при первой компрессии."
                )
            else:  # Full Stop-out (R <= -1.0)
                score = max(1, 3 - int(r_val < -1.2))
                recommendation = "SKIP"
                strengths = ["Локальный технический свип"]
                risks = [
                    "Высокая вероятность ложного пробоя (Bull/Bear Trap)",
                    "Слабая институциональная поддержка или конфликт со старшим трендом",
                ]
                if risk_pct < 0.0025:
                    risks.append(f"Слишком узкий стоп ({risk_pct*100:.2f}%) — уязвим для микро-сквиза")
                reasoning = (
                    f"Сетап {symbol} {direction} является классической розничной ловушкой ликвидности. "
                    f"Высокий риск немедленного выноса стопа. Категорический пропуск сделки."
                )

            user_prompt = f"""Оцени сетап ICT:
- Инструмент: {symbol}
- Направление: {direction}
- Время входа: {row.get('fill_time')}
- Цена входа: {fill_price}
- Стоп-лосс: {stop} (риск: {risk_pct*100:.3f}%)
- Свип азиатской сессии: {is_asian}
- Наличие SMT: {has_smt}"""

            assistant_reply = {
                "score": score,
                "recommendation": recommendation,
                "confluence_strengths": strengths,
                "risk_factors": risks,
                "reasoning": reasoning
            }

            sample = {
                "messages": [
                    {"role": "system", "content": SYSTEM_INSTRUCTION},
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": json.dumps(assistant_reply, ensure_ascii=False)}
                ]
            }
            samples.append(sample)

    with open(output_file, "w", encoding="utf-8") as out:
        for s in samples:
            out.write(json.dumps(s, ensure_ascii=False) + "\n")

    print(f"Датасет для дообучения сформирован: {output_file} (всего примеров: {len(samples)})")
    return len(samples)


if __name__ == "__main__":
    count = build_tuning_dataset()
    print(f"Готово! Сформировано {count} обучающих пар для Gemini API.")
