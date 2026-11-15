"""
EventBridge - Lambda publicadora (POST /runs).

Responsavel por:
- Autorizar a chamada: somente o grupo Cognito "operador" pode publicar eventos.
  A decisao e tomada AQUI, no backend, a partir das claims do JWT ja validado pelo
  JWT Authorizer do API Gateway (event.requestContext.authorizer.jwt.claims).
  Nada enviado pelo navegador (corpo, query string ou cabecalhos) e usado para decidir o perfil.
- Gerar um correlationId por execucao.
- Registrar a execucao (estado CREATED) no DynamoDB ANTES de publicar.
- Publicar os eventos sinteticos de pedido no custom event bus via PutEvents.
- Atualizar a execucao para ACCEPTED / PARTIAL / FAILED com o resultado por entrada.

Observabilidade fiel: um PutEvents aceito NAO significa entrega aos targets.
Quem prova entrega sao os consumidores (itens AUDIT#/PROCESS# no DynamoDB) e os logs.

Variaveis de ambiente:
- EVENT_BUS_NAME : nome do custom event bus (ex.: integracao-lab01-bus)
- EVENT_SOURCE   : source dos eventos (ex.: integracao.lab01.pedidos)
- RUNS_TABLE     : tabela DynamoDB de logs de processo
- CORS_ORIGIN    : origem do dashboard (opcional; ex.: https://dashboard.exemplo.com)
- LOG_LEVEL      : nivel de log (default INFO)
"""
import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "integracao-lab01-bus")
EVENT_SOURCE = os.environ.get("EVENT_SOURCE", "integracao.lab01.pedidos")
RUNS_TABLE = os.environ.get("RUNS_TABLE", "integracao-lab01-runs")
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "")

REQUIRED_GROUP = "operador"

eventbridge = boto3.client("events")
dynamodb = boto3.resource("dynamodb")

SCENARIOS = ("happy", "nomatch", "single")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _response(status_code, body):
    headers = {"Content-Type": "application/json"}
    if CORS_ORIGIN:  # opcional: o CORS da HTTP API tem precedência sobre estes cabeçalhos
        headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Autorizacao por grupo (claims do JWT validado pelo API Gateway)
# ---------------------------------------------------------------------------
def _get_claims(event):
    ctx = (event or {}).get("requestContext") or {}
    jwt = (ctx.get("authorizer") or {}).get("jwt") or {}
    return jwt.get("claims") or {}


def _parse_groups(raw):
    """Normaliza 'cognito:groups'.

    No payload 2.0 do HTTP API a claim de lista chega como string, por exemplo
    '[operador leitor]' ou '["operador","leitor"]'. Tambem aceita lista nativa.
    """
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
    """Retorna (resposta_de_erro | None, claims)."""
    claims = _get_claims(event)
    if not claims:
        return _response(401, {"message": "nao autenticado"}), claims
    if claims.get("token_use") != "access":
        return _response(403, {"message": "use o Access Token do Cognito (token_use=access)"}), claims
    groups = _parse_groups(claims.get("cognito:groups"))
    if REQUIRED_GROUP not in groups:
        logger.info(json.dumps({"msg": "publicacao negada", "sub": claims.get("sub"), "groups": sorted(groups)}))
        return _response(403, {"message": "perfil sem permissao para publicar eventos", "requiredGroup": REQUIRED_GROUP}), claims
    return None, claims


# ---------------------------------------------------------------------------
# Montagem dos eventos
# ---------------------------------------------------------------------------
def _build_entries(correlation_id, scenario):
    """Monta as entradas de PutEvents para o cenario escolhido.

    - happy   : created, paid, cancelled e payment.failed (todos com match)
    - single  : apenas order.created
    - nomatch : order.unknown, que nao casa com nenhuma rule
    """
    base_detail = {
        "schemaVersion": "1.0",
        "correlationId": correlation_id,
        "occurredAt": _now_iso(),
    }
    if scenario == "nomatch":
        selected = ["order.unknown"]
    elif scenario == "single":
        selected = ["order.created"]
    else:
        selected = ["order.created", "order.paid", "order.cancelled", "payment.failed"]

    entries = []
    for idx, event_type in enumerate(selected, start=1):
        detail = dict(base_detail)
        detail["eventType"] = event_type
        detail["data"] = {
            "orderId": f"order-{correlation_id[-4:]}-{idx:02d}",
            "amount": round(100 + idx * 25.9, 2),
        }
        if event_type == "payment.failed":
            # Campo usado pelo Input Transformer ($.detail.data.reason).
            detail["data"]["reason"] = "card_declined"
        entries.append(
            {
                "Source": EVENT_SOURCE,
                "DetailType": event_type,
                "Detail": json.dumps(detail),
                "EventBusName": EVENT_BUS_NAME,
            }
        )
    return entries, selected


def _parse_body(event):
    raw = (event or {}).get("body")
    if raw is None or raw == "":
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("corpo deve ser um objeto JSON")
    return data


def handler(event, context):
    """Entry point (integracao proxy, payload 2.0 do HTTP API)."""
    denied, claims = _authorize(event)
    if denied:
        return denied

    request_id = getattr(context, "aws_request_id", None)
    try:
        try:
            body = _parse_body(event)
        except (ValueError, UnicodeDecodeError):
            return _response(400, {"message": "corpo invalido: envie JSON, ex.: {\"scenario\":\"happy\"}"})

        scenario = body.get("scenario", "happy")
        if scenario not in SCENARIOS:
            return _response(400, {"message": "scenario invalido", "allowed": list(SCENARIOS)})

        correlation_id = f"lab01-run-{uuid.uuid4().hex[:8]}"
        entries, event_types = _build_entries(correlation_id, scenario)
        table = dynamodb.Table(RUNS_TABLE)

        # 1) Registra a intencao ANTES de publicar (estado CREATED).
        table.put_item(
            Item={
                "runId": correlation_id,
                "sk": "RUN",
                "correlationId": correlation_id,
                "scenario": scenario,
                "eventTypes": event_types,
                "state": "CREATED",
                "createdAt": _now_iso(),
                "createdBy": claims.get("username") or claims.get("sub", "desconhecido"),
                "entryCount": len(entries),
            },
            ConditionExpression="attribute_not_exists(runId)",
        )
        logger.info(json.dumps({"msg": "publicando eventos", "correlationId": correlation_id, "scenario": scenario, "count": len(entries)}))

        # 2) Publica no bus.
        try:
            result = eventbridge.put_events(Entries=entries)
        except Exception:
            table.update_item(
                Key={"runId": correlation_id, "sk": "RUN"},
                UpdateExpression="SET #s = :s, acceptedCount = :a, failedCount = :f, publishedAt = :t",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={":s": "FAILED", ":a": 0, ":f": len(entries), ":t": _now_iso()},
            )
            raise

        failed = result.get("FailedEntryCount", 0)
        accepted = len(entries) - failed
        entry_results = []
        for event_type, res in zip(event_types, result.get("Entries", [])):
            item = {"eventType": event_type}
            if res.get("EventId"):
                item["eventId"] = res["EventId"]
            if res.get("ErrorCode"):
                item["errorCode"] = res["ErrorCode"]
            entry_results.append(item)

        state = "ACCEPTED" if failed == 0 else ("FAILED" if accepted == 0 else "PARTIAL")

        # 3) Atualiza o resultado da publicacao (NAO e prova de entrega).
        table.update_item(
            Key={"runId": correlation_id, "sk": "RUN"},
            UpdateExpression="SET #s = :s, acceptedCount = :a, failedCount = :f, entryResults = :r, publishedAt = :t",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":s": state,
                ":a": accepted,
                ":f": failed,
                ":r": entry_results,
                ":t": _now_iso(),
            },
        )
        logger.info(json.dumps({"msg": "PutEvents concluido", "correlationId": correlation_id, "accepted": accepted, "failed": failed}))

        return _response(
            202 if accepted > 0 else 502,
            {
                "correlationId": correlation_id,
                "runId": correlation_id,
                "scenario": scenario,
                "state": state,
                "accepted": accepted,
                "failed": failed,
                "entryResults": entry_results,
                "note": "Aceite no bus nao garante entrega aos targets. Acompanhe o estado observado no dashboard.",
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("Falha ao publicar eventos")
        return _response(500, {"message": "erro interno", "requestId": request_id})
