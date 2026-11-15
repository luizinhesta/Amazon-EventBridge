"""
EventBridge - Lambda leitura (GET /runs, /runs/{id}, /runs/{id}/events, /metrics).

Autorizacao: exige JWT valido (API Gateway) E pertencimento a "operador" ou "leitor",
verificado aqui a partir das claims (event.requestContext.authorizer.jwt.claims).

Observabilidade fiel: o estado observado so reflete EVIDENCIAS encontradas:
- DynamoDB : itens PROCESS#<tipo> (Lambda de processamento) e AUDIT#<tipo> (Lambda auditora).
- CloudWatch Logs : linha gravada pelo target de logs (rule payment.failed) com o correlationId.
- SNS : NAO existe evidencia automatica (e-mail). Fica "nao_verificavel" e nunca conta como confirmado.
O aceite no PutEvents (item RUN) nunca e tratado como entrega.

Estados observados (observedState):
- AGUARDANDO        : nenhuma evidencia ainda, dentro da janela de tolerancia.
- PARCIAL           : parte das evidencias esperadas, dentro da janela.
- CONFIRMADO        : todas as evidencias verificaveis esperadas foram encontradas.
- INCOMPLETO        : janela expirou e faltam algumas evidencias esperadas.
- SEM_EVIDENCIA     : janela expirou e nenhuma evidencia esperada foi encontrada.
- FALHA             : falha de publicacao ou evidencia com estado FAILED.
- SEM_DESTINO       : cenario nomatch sem evidencia (resultado esperado).
- INESPERADO        : ha evidencia onde nenhum destino era esperado.
- NAO_VERIFICAVEL   : todos os destinos esperados nao tem evidencia automatica.
- SEM_REGISTRO_PUBLICACAO : ha evidencia, mas nao ha item RUN (ex.: replay antigo).

Variaveis de ambiente:
- RUNS_TABLE, CORS_ORIGIN, LOG_LEVEL
- EVENT_BUS_NAME        (default integracao-lab01-bus)
- RULE_NAMES            (default: as 4 rules do lab, separadas por virgula)
- EVENTS_LOG_GROUP      (default /aws/events/integracao-lab01)
- TARGET_DLQ_NAME       (default integracao-lab01-target-dlq)
- EVIDENCE_GRACE_SECONDS (default 120)
- LOGS_LOOKBACK_HOURS   (default 24)
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

RUNS_TABLE = os.environ.get("RUNS_TABLE", "integracao-lab01-runs")
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "integracao-lab01-bus")
RULE_NAMES = [
    r.strip()
    for r in os.environ.get(
        "RULE_NAMES",
        "integracao-lab01-rule-created,integracao-lab01-rule-paid,"
        "integracao-lab01-rule-cancelled,integracao-lab01-rule-payment-failed",
    ).split(",")
    if r.strip()
]
EVENTS_LOG_GROUP = os.environ.get("EVENTS_LOG_GROUP", "/aws/events/integracao-lab01")
TARGET_DLQ_NAME = os.environ.get("TARGET_DLQ_NAME", "integracao-lab01-target-dlq")
GRACE_SECONDS = int(os.environ.get("EVIDENCE_GRACE_SECONDS", "120"))
LOGS_LOOKBACK_HOURS = int(os.environ.get("LOGS_LOOKBACK_HOURS", "24"))

ALLOWED_GROUPS = {"operador", "leitor"}

dynamodb = boto3.resource("dynamodb")
logs_client = boto3.client("logs")
cloudwatch = boto3.client("cloudwatch")
sqs = boto3.client("sqs")

# Destinos esperados por tipo de evento. "evidence" indica de onde vem a prova.
EXPECTED_BY_EVENT = {
    "order.created": {"target": "processor", "evidence": "dynamodb", "sk": "PROCESS#order.created"},
    "order.paid": {"target": "auditor", "evidence": "dynamodb", "sk": "AUDIT#order.paid"},
    "order.cancelled": {"target": "sns", "evidence": None, "sk": None},
    "payment.failed": {"target": "cloudwatch-logs", "evidence": "logs", "sk": None},
}
# Fallback para execucoes antigas sem o campo eventTypes.
SCENARIO_EVENT_TYPES = {
    "happy": ["order.created", "order.paid", "order.cancelled", "payment.failed"],
    "single": ["order.created"],
    "nomatch": ["order.unknown"],
}

EB_METRICS = [
    "Invocations",
    "FailedInvocations",
    "TriggeredRules",
    "MatchedEvents",
    "ThrottledRules",
    "InvocationsSentToDlq",
    "InvocationsFailedToBeSentToDlq",
    "DeadLetterInvocations",
    "RetryInvocationAttempts",
]

CORRELATION_RE = re.compile(r'correlationId\\?"?\s*[:=]\s*\\?"?([A-Za-z0-9_-]+)')
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


# ---------------------------------------------------------------------------
# Utilitarios
# ---------------------------------------------------------------------------
class _JsonEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj == obj.to_integral_value() else float(obj)
        if isinstance(obj, set):
            return sorted(obj)
        return super().default(obj)


def _response(status_code, body):
    headers = {"Content-Type": "application/json"}
    if CORS_ORIGIN:  # opcional: o CORS da HTTP API tem precedência sobre estes cabeçalhos
        headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body, cls=_JsonEncoder),
    }


def _now():
    return datetime.now(timezone.utc)


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _get_claims(event):
    ctx = (event or {}).get("requestContext") or {}
    return ((ctx.get("authorizer") or {}).get("jwt") or {}).get("claims") or {}


def _parse_groups(raw):
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        text = str(raw).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        items = re.split(r"[,\s]+", text)
    return {str(i).strip().strip("\"'") for i in items if str(i).strip().strip("\"'")}


def _authorize(event):
    claims = _get_claims(event)
    if not claims:
        return _response(401, {"message": "nao autenticado"})
    if claims.get("token_use") != "access":
        return _response(403, {"message": "use o Access Token do Cognito (token_use=access)"})
    if not (_parse_groups(claims.get("cognito:groups")) & ALLOWED_GROUPS):
        return _response(403, {"message": "perfil sem permissao de consulta", "allowedGroups": sorted(ALLOWED_GROUPS)})
    return None


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------
def _scan_all(table):
    # Scan e aceitavel no volume pequeno do projeto; em producao usar indice/paginacao.
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _query_run(table, run_id):
    items, kwargs = [], {"KeyConditionExpression": Key("runId").eq(run_id)}
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _group_by_run(items):
    grouped = {}
    for item in items:
        grouped.setdefault(item.get("runId"), []).append(item)
    return grouped


# ---------------------------------------------------------------------------
# CloudWatch Logs (evidencia do target de logs)
# ---------------------------------------------------------------------------
def _logs_index(start_ms, run_id=None):
    """Retorna ({correlationId: [timestamps_ms]}, disponivel: bool)."""
    index = {}
    kwargs = {"logGroupName": EVENTS_LOG_GROUP, "startTime": int(start_ms), "limit": 10000}
    if run_id:
        kwargs["filterPattern"] = f'"{run_id}"'
    try:
        for _ in range(5):  # limite de paginas para controlar custo/tempo
            resp = logs_client.filter_log_events(**kwargs)
            for ev in resp.get("events", []):
                match = CORRELATION_RE.search(ev.get("message", ""))
                if match:
                    index.setdefault(match.group(1), []).append(ev["timestamp"])
            token = resp.get("nextToken")
            if not token:
                break
            kwargs["nextToken"] = token
        return index, True
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"msg": "logs indisponiveis", "error": type(exc).__name__}))
        return {}, False


# ---------------------------------------------------------------------------
# Montagem da visao de uma execucao
# ---------------------------------------------------------------------------
def _iso_from_ms(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _build_view(run_id, items, logs_idx, logs_ok, now):
    run = next((i for i in items if i.get("sk") == "RUN"), None)
    ddb_evidence = [i for i in items if str(i.get("sk", "")).startswith(("AUDIT#", "PROCESS#"))]

    evidences = []
    for item in ddb_evidence:
        evidences.append(
            {
                "target": item.get("target"),
                "eventType": item.get("eventType"),
                "state": item.get("state", "SUCCEEDED"),
                "observedAt": item.get("observedAt"),
                "source": "dynamodb",
                "sk": item.get("sk"),
            }
        )
    if logs_ok and run_id in logs_idx:
        stamps = sorted(logs_idx[run_id])
        evidences.append(
            {
                "target": "cloudwatch-logs",
                "eventType": "payment.failed",
                "state": "SUCCEEDED",
                "observedAt": _iso_from_ms(stamps[0]),
                "source": "cloudwatch-logs",
                "occurrences": len(stamps),
            }
        )

    if run:
        origin = "api"
        event_types = run.get("eventTypes") or SCENARIO_EVENT_TYPES.get(run.get("scenario"), [])
        scenario = run.get("scenario", "-")
        created_at = run.get("createdAt")
    elif run_id.startswith("lab01-sched-"):
        origin = "agendador"
        event_types = ["order.created"]
        scenario = "agendado"
        created_at = min((e["observedAt"] for e in evidences if e.get("observedAt")), default=None)
    else:
        origin = "desconhecida"
        event_types = []
        scenario = "-"
        created_at = min((e["observedAt"] for e in evidences if e.get("observedAt")), default=None)

    created_dt = _parse_dt(created_at)
    age = (now - created_dt).total_seconds() if created_dt else None
    within_grace = age is not None and age <= GRACE_SECONDS

    ev_by_key = {(e["target"], e["eventType"]): e for e in evidences}
    expected, expected_keys = [], set()
    for event_type in event_types:
        spec = EXPECTED_BY_EVENT.get(event_type)
        if not spec:
            continue  # ex.: order.unknown (nomatch) nao tem destino esperado
        key = (spec["target"], event_type)
        expected_keys.add(key)
        if spec["evidence"] is None or (spec["evidence"] == "logs" and not logs_ok):
            status = "nao_verificavel"
        elif key in ev_by_key:
            status = "falha" if ev_by_key[key]["state"] == "FAILED" else "confirmado"
        else:
            status = "aguardando" if within_grace else "ausente"
        expected.append({"eventType": event_type, "target": spec["target"], "status": status})

    unexpected = [e for e in evidences if (e["target"], e["eventType"]) not in expected_keys]
    unverifiable = [x["target"] for x in expected if x["status"] == "nao_verificavel"]

    observed = _derive_state(run, origin, expected, evidences, unexpected, within_grace)

    view = {
        "runId": run_id,
        "correlationId": run_id,
        "origin": origin,
        "scenario": scenario,
        "state": (run or {}).get("state"),  # estado da PUBLICACAO (nao e entrega)
        "createdAt": created_at,
        "createdBy": (run or {}).get("createdBy"),
        "acceptedCount": (run or {}).get("acceptedCount"),
        "failedCount": (run or {}).get("failedCount"),
        "entryCount": (run or {}).get("entryCount"),
        "entryResults": (run or {}).get("entryResults"),
        "observedState": observed,
        "evidenceCount": len(evidences),
        "evidences": evidences,
        "expected": expected,
        "unverifiableTargets": unverifiable,
        "graceSeconds": GRACE_SECONDS,
    }
    return view


def _derive_state(run, origin, expected, evidences, unexpected, within_grace):
    if run is not None:
        failed_count = run.get("failedCount") or 0
        if run.get("state") == "FAILED" or failed_count > 0:
            return "FALHA"
        if run.get("state") == "CREATED" and not evidences and not within_grace:
            return "SEM_EVIDENCIA"  # registrada mas nunca concluiu a publicacao
    elif origin == "desconhecida":
        return "SEM_REGISTRO_PUBLICACAO"

    if any(e["state"] == "FAILED" for e in evidences) or any(x["status"] == "falha" for x in expected):
        return "FALHA"

    if not expected:
        return "INESPERADO" if unexpected else "SEM_DESTINO"

    verifiable = [x for x in expected if x["status"] != "nao_verificavel"]
    if not verifiable:
        return "NAO_VERIFICAVEL"

    confirmed = [x for x in verifiable if x["status"] == "confirmado"]
    if len(confirmed) == len(verifiable):
        return "CONFIRMADO"
    if within_grace:
        return "PARCIAL" if confirmed else "AGUARDANDO"
    return "INCOMPLETO" if confirmed else "SEM_EVIDENCIA"


def _all_views(table, now):
    grouped = _group_by_run(_scan_all(table))
    start_ms = (now - timedelta(hours=LOGS_LOOKBACK_HOURS)).timestamp() * 1000
    logs_idx, logs_ok = _logs_index(start_ms)
    views = []
    for run_id, items in grouped.items():
        if not run_id:
            continue
        has_run = any(i.get("sk") == "RUN" for i in items)
        if not has_run and not str(run_id).startswith("lab01-sched-"):
            continue  # evidencia orfa que nao e do agendador: nao listar como execucao
        views.append(_build_view(run_id, items, logs_idx, logs_ok, now))
    views.sort(key=lambda v: v.get("createdAt") or "", reverse=True)
    return views


# ---------------------------------------------------------------------------
# Metricas
# ---------------------------------------------------------------------------
def _eventbridge_metrics(hours):
    """Metricas reais do namespace AWS/Events. Sem dado => None (nunca zero inventado)."""
    result = {"windowHours": hours, "available": False, "rules": {}, "error": None}
    for rule in RULE_NAMES:
        result["rules"][rule] = {m: None for m in EB_METRICS}
    try:
        series = []
        for metric_name in EB_METRICS:
            paginator = cloudwatch.get_paginator("list_metrics")
            for page in paginator.paginate(Namespace="AWS/Events", MetricName=metric_name):
                for metric in page.get("Metrics", []):
                    dims = {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}
                    rule = dims.get("RuleName")
                    bus = dims.get("EventBusName")
                    if rule in RULE_NAMES and bus in (None, EVENT_BUS_NAME):
                        series.append((metric_name, rule, metric["Dimensions"]))
        end = _now()
        start = end - timedelta(hours=hours)
        totals = {}
        for offset in range(0, len(series), 400):
            chunk = series[offset: offset + 400]
            queries = [
                {
                    "Id": f"m{offset + n}",
                    "MetricStat": {
                        "Metric": {"Namespace": "AWS/Events", "MetricName": name, "Dimensions": dims},
                        "Period": 3600,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                }
                for n, (name, _rule, dims) in enumerate(chunk)
            ]
            kwargs = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end}
            while True:
                resp = cloudwatch.get_metric_data(**kwargs)
                for res in resp.get("MetricDataResults", []):
                    idx = int(res["Id"][1:]) - offset
                    name, rule, _dims = chunk[idx]
                    values = res.get("Values", [])
                    if values:
                        totals[(rule, name)] = totals.get((rule, name), 0) + sum(values)
                token = resp.get("NextToken")
                if not token:
                    break
                kwargs["NextToken"] = token
        for (rule, name), value in totals.items():
            result["rules"][rule][name] = value
        result["available"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"msg": "metricas EventBridge indisponiveis", "error": type(exc).__name__}))
        result["error"] = type(exc).__name__
    return result


def _dlq_depth():
    try:
        url = sqs.get_queue_url(QueueName=TARGET_DLQ_NAME)["QueueUrl"]
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return {
            "queue": TARGET_DLQ_NAME,
            "available": True,
            "messages": int(attrs.get("ApproximateNumberOfMessages", 0)),
            "notVisible": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"msg": "DLQ indisponivel", "error": type(exc).__name__}))
        return {"queue": TARGET_DLQ_NAME, "available": False, "error": type(exc).__name__}


def _int_param(event, name, default, minimum, maximum):
    raw = (event.get("queryStringParameters") or {}).get(name)
    try:
        return max(minimum, min(maximum, int(raw)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def handler(event, context):
    denied = _authorize(event)
    if denied:
        return denied

    method = ((event.get("requestContext") or {}).get("http") or {}).get("method") or event.get("httpMethod") or "GET"
    if method.upper() != "GET":
        return _response(405, {"message": "metodo nao permitido"})

    path = (event.get("rawPath") or event.get("path") or "").rstrip("/")
    run_id = (event.get("pathParameters") or {}).get("id")
    if run_id is not None and not RUN_ID_RE.match(run_id):
        return _response(400, {"message": "id invalido"})

    table = dynamodb.Table(RUNS_TABLE)
    now = _now()
    request_id = getattr(context, "aws_request_id", None)

    try:
        if path.endswith("/metrics"):
            views = [v for v in _all_views(table, now) if v["origin"] == "api"]
            by_state = {}
            for v in views:
                by_state[v["observedState"]] = by_state.get(v["observedState"], 0) + 1
            hours = _int_param(event, "hours", 24, 1, 168)
            return _response(
                200,
                {
                    "totalRuns": len(views),
                    "acceptedEvents": sum(v.get("acceptedCount") or 0 for v in views),
                    "failedEvents": sum(v.get("failedCount") or 0 for v in views),
                    "observedStates": by_state,
                    "eventbridge": _eventbridge_metrics(hours),
                    "targetDlq": _dlq_depth(),
                },
            )

        if run_id:
            items = _query_run(table, run_id)
            start_ms = (now - timedelta(hours=LOGS_LOOKBACK_HOURS)).timestamp() * 1000
            run_item = next((i for i in items if i.get("sk") == "RUN"), None)
            created = _parse_dt((run_item or {}).get("createdAt"))
            if created:
                start_ms = (created - timedelta(minutes=1)).timestamp() * 1000
            logs_idx, logs_ok = _logs_index(start_ms, run_id)
            if not items and run_id not in logs_idx:
                return _response(404, {"message": "execucao nao encontrada", "runId": run_id})
            view = _build_view(run_id, items, logs_idx, logs_ok, now)
            if path.endswith("/events"):
                return _response(200, {"runId": run_id, "events": view["evidences"], "expected": view["expected"]})
            return _response(200, {"runId": run_id, "run": view})

        limit = _int_param(event, "limit", 50, 1, 200)
        views = _all_views(table, now)
        return _response(200, {"runs": views[:limit], "total": len(views), "returned": min(limit, len(views))})
    except Exception:  # noqa: BLE001
        logger.exception("Falha na leitura")
        return _response(500, {"message": "erro interno", "requestId": request_id})
