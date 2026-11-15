/*
 * EventBridge - logica do dashboard.
 *
 * Autenticacao: Amazon Cognito, OAuth 2.0 Authorization Code Flow com PKCE (S256),
 * App Client publico (sem client secret). Nenhum token ou segredo e escrito em arquivo,
 * console ou DOM. Os tokens ficam apenas em sessionStorage (apagado ao fechar a aba).
 * A decodificacao do JWT aqui serve SOMENTE para exibir usuario/perfil; a autorizacao
 * real (operador x leitor) e feita no backend com as claims validadas pelo API Gateway.
 *
 * Observabilidade fiel: sem API configurada (ou MODO_DEMONSTRACAO = true) roda em MODO
 * DEMONSTRACAO com dados sinteticos rotulados. Com API configurada usa EXCLUSIVAMENTE
 * dados reais, sem mesclar dados de demonstracao.
 */
(function () {
  "use strict";

  var cfg = Object.assign({}, window.LAB01_CONFIG || {});
  // Valores ainda no formato <VARIAVEL> (não preenchidos) são tratados como vazios.
  ["API_BASE_URL", "COGNITO_DOMAIN", "COGNITO_CLIENT_ID", "REDIRECT_URI", "LOGOUT_URI"].forEach(function (k) {
    if (typeof cfg[k] === "string" && cfg[k].indexOf("<") >= 0) cfg[k] = "";
  });
  var MODO_DEMO = !cfg.API_BASE_URL || cfg.MODO_DEMONSTRACAO === true;
  var API = (cfg.API_BASE_URL || "").replace(/\/+$/, "");
  var COGNITO = (cfg.COGNITO_DOMAIN || "").replace(/\/+$/, "");
  var AUTH_CONFIGURADA = !!(COGNITO && cfg.COGNITO_CLIENT_ID && cfg.REDIRECT_URI);
  var KEY_SESSION = "lab01.session";
  var KEY_PKCE = "lab01.pkce";
  var MARGEM_RENOVACAO_S = 60;

  var $ = function (id) { return document.getElementById(id); };
  var elBtn = $("btn-disparar");
  var elCenario = $("cenario");
  var elStatus = $("status-disparo");
  var elRunsBody = $("runs-body");
  var elBanner = $("modo-banner");
  var elLogin = $("btn-login");
  var elLogout = $("btn-logout");
  var elUsuario = $("auth-usuario");
  var elPerfil = $("auth-perfil");
  var elExpira = $("auth-expira");
  var elAuthMsg = $("auth-mensagem");

  var ROTULOS_ESTADO = {
    AGUARDANDO: "Aguardando evidência",
    PARCIAL: "Parcial (aguardando o restante)",
    CONFIRMADO: "Entrega confirmada (destinos verificáveis)",
    INCOMPLETO: "Incompleto (evidência ausente)",
    SEM_EVIDENCIA: "Sem evidência",
    FALHA: "Falha",
    SEM_DESTINO: "Sem destino (esperado: no match)",
    INESPERADO: "Evidência inesperada",
    NAO_VERIFICAVEL: "Não verificável automaticamente",
    SEM_REGISTRO_PUBLICACAO: "Evidência sem registro de publicação",
    DESCONHECIDO: "Desconhecido"
  };
  var ROTULOS_STATUS = {
    confirmado: "confirmado",
    aguardando: "aguardando",
    ausente: "ausente (fora da janela)",
    falha: "falha",
    nao_verificavel: "não verificável (confirmar manualmente)"
  };
  var METRICAS_EB = ["MatchedEvents", "TriggeredRules", "Invocations", "FailedInvocations",
    "InvocationsSentToDlq", "InvocationsFailedToBeSentToDlq", "RetryInvocationAttempts", "ThrottledRules"];

  // Dados sinteticos usados SOMENTE no modo demonstracao.
  var demoRuns = [
    {
      runId: "lab01-run-demo0001", correlationId: "lab01-run-demo0001", scenario: "happy", state: "ACCEPTED",
      createdAt: "2026-11-03T20:00:00Z", acceptedCount: 4, failedCount: 0, observedState: "CONFIRMADO", evidenceCount: 3,
      evidences: [
        { target: "processor", eventType: "order.created", state: "SUCCEEDED", observedAt: "2026-11-03T20:00:02Z", source: "dynamodb" },
        { target: "auditor", eventType: "order.paid", state: "SUCCEEDED", observedAt: "2026-11-03T20:00:02Z", source: "dynamodb" },
        { target: "cloudwatch-logs", eventType: "payment.failed", state: "SUCCEEDED", observedAt: "2026-11-03T20:00:03Z", source: "cloudwatch-logs" }
      ],
      expected: [
        { eventType: "order.created", target: "processor", status: "confirmado" },
        { eventType: "order.paid", target: "auditor", status: "confirmado" },
        { eventType: "order.cancelled", target: "sns", status: "nao_verificavel" },
        { eventType: "payment.failed", target: "cloudwatch-logs", status: "confirmado" }
      ],
      unverifiableTargets: ["sns"]
    },
    {
      runId: "lab01-run-demo0002", correlationId: "lab01-run-demo0002", scenario: "nomatch", state: "ACCEPTED",
      createdAt: "2026-11-03T20:03:00Z", acceptedCount: 1, failedCount: 0, observedState: "SEM_DESTINO", evidenceCount: 0,
      evidences: [], expected: [], unverifiableTargets: []
    }
  ];

  // ---------------------------------------------------------------------------
  // Utilitarios de DOM seguros (textContent; nunca innerHTML com dados externos)
  // ---------------------------------------------------------------------------
  function el(tag, props, children) {
    var n = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      if (k === "text") n.textContent = props[k]; else n.setAttribute(k, props[k]);
    });
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }
  function limpar(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function fmtData(iso) {
    if (!iso) return "-";
    var d = new Date(iso);
    return isNaN(d.getTime()) ? "-" : d.toLocaleString("pt-BR");
  }
  function setMsg(node, msg, tipo) {
    node.textContent = msg || "";
    node.className = "status" + (tipo ? " " + tipo : "");
  }

  // ---------------------------------------------------------------------------
  // PKCE / Cognito
  // ---------------------------------------------------------------------------
  function b64url(buf) {
    var s = "";
    new Uint8Array(buf).forEach(function (b) { s += String.fromCharCode(b); });
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function aleatorio(bytes) {
    var a = new Uint8Array(bytes);
    crypto.getRandomValues(a);
    return b64url(a);
  }
  function sha256(texto) {
    return crypto.subtle.digest("SHA-256", new TextEncoder().encode(texto));
  }
  function decodeJwt(token) {
    try {
      var p = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      while (p.length % 4) p += "=";
      var json = decodeURIComponent(atob(p).split("").map(function (c) {
        return "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2);
      }).join(""));
      return JSON.parse(json);
    } catch (e) { return {}; }
  }
  function lerSessao() {
    try { return JSON.parse(sessionStorage.getItem(KEY_SESSION) || "null"); } catch (e) { return null; }
  }
  function salvarSessao(s) { sessionStorage.setItem(KEY_SESSION, JSON.stringify(s)); }
  function limparSessao() { sessionStorage.removeItem(KEY_SESSION); }

  function postToken(params) {
    return fetch(COGNITO + "/oauth2/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(params).toString()
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
        return body;
      });
    });
  }
  function guardarTokens(tokens, refreshAnterior) {
    var s = {
      accessToken: tokens.access_token,
      idToken: tokens.id_token || (lerSessao() || {}).idToken,
      // O Cognito nao devolve novo refresh_token no grant refresh_token (sem rotacao).
      refreshToken: tokens.refresh_token || refreshAnterior,
      expiresAt: Date.now() + (Number(tokens.expires_in) || 3600) * 1000
    };
    salvarSessao(s);
    return s;
  }

  function iniciarLogin() {
    if (!AUTH_CONFIGURADA) {
      setMsg(elAuthMsg, "Autenticação não configurada: preencha COGNITO_DOMAIN e COGNITO_CLIENT_ID em config.js.", "erro");
      return;
    }
    var verifier = aleatorio(64);
    var state = aleatorio(24);
    var nonce = aleatorio(24);
    sha256(verifier).then(function (hash) {
      sessionStorage.setItem(KEY_PKCE, JSON.stringify({ verifier: verifier, state: state, nonce: nonce }));
      var q = new URLSearchParams({
        response_type: "code",
        client_id: cfg.COGNITO_CLIENT_ID,
        redirect_uri: cfg.REDIRECT_URI,
        scope: cfg.SCOPES || "openid email profile",
        state: state,
        nonce: nonce,
        code_challenge: b64url(hash),
        code_challenge_method: "S256"
      });
      window.location.assign(COGNITO + "/oauth2/authorize?" + q.toString());
    }).catch(function () {
      setMsg(elAuthMsg, "Este navegador não suporta Web Crypto (use HTTPS).", "erro");
    });
  }

  // Trata o retorno do Cognito (?code=...&state=...). Retorna Promise.
  function tratarCallback() {
    var params = new URLSearchParams(window.location.search);
    if (!params.has("code") && !params.has("error")) return Promise.resolve();
    var limparUrl = function () { window.history.replaceState({}, document.title, window.location.pathname); };

    if (params.has("error")) {
      var desc = params.get("error_description") || params.get("error");
      limparUrl();
      sessionStorage.removeItem(KEY_PKCE);
      setMsg(elAuthMsg, "Login recusado: " + desc, "erro");
      return Promise.resolve();
    }
    var code = params.get("code");
    var stateRecebido = params.get("state");
    var salvo;
    try { salvo = JSON.parse(sessionStorage.getItem(KEY_PKCE) || "null"); } catch (e) { salvo = null; }
    sessionStorage.removeItem(KEY_PKCE);
    limparUrl(); // o codigo e de uso unico e nao deve ficar na barra de enderecos

    if (!salvo || !stateRecebido || stateRecebido !== salvo.state) {
      setMsg(elAuthMsg, "Retorno de login inválido (state divergente). Tente entrar novamente.", "erro");
      return Promise.resolve();
    }
    return postToken({
      grant_type: "authorization_code",
      client_id: cfg.COGNITO_CLIENT_ID,
      code: code,
      redirect_uri: cfg.REDIRECT_URI,
      code_verifier: salvo.verifier
    }).then(function (tokens) {
      if (tokens.id_token && decodeJwt(tokens.id_token).nonce !== salvo.nonce) {
        throw new Error("nonce divergente");
      }
      guardarTokens(tokens);
      setMsg(elAuthMsg, "Login concluído.", "ok");
    }).catch(function (err) {
      limparSessao();
      setMsg(elAuthMsg, "Falha na troca do código por tokens: " + err.message, "erro");
    });
  }

  var renovando = null;
  function renovar(sessao) {
    if (!sessao || !sessao.refreshToken) return Promise.reject(new Error("sem refresh token"));
    if (!renovando) {
      renovando = postToken({
        grant_type: "refresh_token",
        client_id: cfg.COGNITO_CLIENT_ID,
        refresh_token: sessao.refreshToken
      }).then(function (tokens) {
        return guardarTokens(tokens, sessao.refreshToken);
      }).finally(function () { renovando = null; });
    }
    return renovando;
  }
  // Retorna Promise<string|null> com Access Token valido (renova se necessario).
  function tokenValido() {
    var s = lerSessao();
    if (!s || !s.accessToken) return Promise.resolve(null);
    if (s.expiresAt - Date.now() > MARGEM_RENOVACAO_S * 1000) return Promise.resolve(s.accessToken);
    return renovar(s).then(function (n) { return n.accessToken; }).catch(function () {
      limparSessao();
      setMsg(elAuthMsg, "Sessão expirada. Entre novamente.", "erro");
      atualizarUiAuth();
      return null;
    });
  }
  function sair() {
    limparSessao();
    if (!AUTH_CONFIGURADA) { atualizarUiAuth(); return; }
    var q = new URLSearchParams({ client_id: cfg.COGNITO_CLIENT_ID, logout_uri: cfg.LOGOUT_URI });
    window.location.assign(COGNITO + "/logout?" + q.toString());
  }

  function perfilAtual() {
    var s = lerSessao();
    if (!s || !s.accessToken) return null;
    var acc = decodeJwt(s.accessToken);
    var idt = s.idToken ? decodeJwt(s.idToken) : {};
    var grupos = acc["cognito:groups"] || idt["cognito:groups"] || [];
    if (!Array.isArray(grupos)) grupos = String(grupos).replace(/[\[\]"]/g, "").split(/[,\s]+/).filter(Boolean);
    var perfil = grupos.indexOf("operador") >= 0 ? "operador" : (grupos.indexOf("leitor") >= 0 ? "leitor" : "sem perfil");
    return { usuario: idt.email || acc.username || "usuário", perfil: perfil, expiresAt: s.expiresAt };
  }
  function podePublicar() { var p = perfilAtual(); return !!p && p.perfil === "operador"; }

  function atualizarUiAuth() {
    var p = perfilAtual();
    limpar(elPerfil);
    if (MODO_DEMO) {
      elUsuario.textContent = "Modo demonstração (sem autenticação)";
      elLogin.hidden = true; elLogout.hidden = true; elExpira.textContent = "";
    } else if (p) {
      elUsuario.textContent = p.usuario;
      elPerfil.appendChild(document.createTextNode("Perfil: "));
      elPerfil.appendChild(el("span", { "class": "perfil " + (p.perfil === "sem perfil" ? "" : p.perfil), text: p.perfil }));
      elExpira.textContent = "Access Token válido até " + new Date(p.expiresAt).toLocaleTimeString("pt-BR") + " (renovação automática quando possível)";
      elLogin.hidden = true; elLogout.hidden = false;
    } else {
      elUsuario.textContent = "Não autenticado";
      elExpira.textContent = "";
      elLogin.hidden = false; elLogout.hidden = true;
      elLogin.disabled = !AUTH_CONFIGURADA;
      if (!AUTH_CONFIGURADA && !elAuthMsg.textContent) {
        setMsg(elAuthMsg, "Autenticação não configurada: preencha COGNITO_DOMAIN e COGNITO_CLIENT_ID em config.js.", "erro");
      }
    }
    elBtn.disabled = MODO_DEMO ? false : !podePublicar();
    elBtn.title = (!MODO_DEMO && !podePublicar()) ? "Somente o perfil operador pode publicar eventos" : "";
  }

  // ---------------------------------------------------------------------------
  // Chamadas a API (Access Token no header Authorization)
  // ---------------------------------------------------------------------------
  function apiFetch(path, opts, tentouRenovar) {
    return tokenValido().then(function (token) {
      if (!token) { var e = new Error("não autenticado"); e.status = 401; throw e; }
      var o = opts || {};
      o.headers = Object.assign({ "Content-Type": "application/json", "Authorization": "Bearer " + token }, o.headers || {});
      return fetch(API + path, o);
    }).then(function (res) {
      if (res.status === 401 && !tentouRenovar) {
        var s = lerSessao();
        return renovar(s).then(function () { return apiFetch(path, opts, true); }).catch(function () {
          limparSessao(); atualizarUiAuth();
          var e = new Error("sessão inválida ou expirada (HTTP 401)"); e.status = 401; throw e;
        });
      }
      return res;
    });
  }
  function jsonOuErro(res) {
    return res.json().catch(function () { return {}; }).then(function (body) {
      if (!res.ok) {
        var e = new Error(body.message || ("HTTP " + res.status));
        e.status = res.status; throw e;
      }
      return body;
    });
  }

  // ---------------------------------------------------------------------------
  // Renderizacao
  // ---------------------------------------------------------------------------
  function estadoCelula(estado) {
    var chave = ROTULOS_ESTADO[estado] ? estado : "DESCONHECIDO";
    return el("td", { "class": "estado " + chave, title: ROTULOS_ESTADO[chave] }, [document.createTextNode(ROTULOS_ESTADO[chave])]);
  }
  function evidenciasCelula(r) {
    var n = typeof r.evidenceCount === "number" ? r.evidenceCount : 0;
    var det = el("details", {}, [el("summary", { text: n + " evidência(s)" })]);
    var ul1 = el("ul");
    (r.expected || []).forEach(function (x) {
      ul1.appendChild(el("li", { "class": "st-" + x.status, text: "esperado: " + x.eventType + " → " + x.target + ": " + (ROTULOS_STATUS[x.status] || x.status) }));
    });
    if (!(r.expected || []).length) ul1.appendChild(el("li", { text: "nenhum destino esperado para este cenário" }));
    det.appendChild(ul1);
    var ul2 = el("ul");
    (r.evidences || []).forEach(function (e) {
      ul2.appendChild(el("li", { text: "encontrado: " + e.eventType + " → " + e.target + " [" + e.state + "] " + fmtData(e.observedAt) + " (" + e.source + ")" }));
    });
    if (!(r.evidences || []).length) ul2.appendChild(el("li", { text: "nenhuma evidência encontrada até agora" }));
    det.appendChild(ul2);
    return el("td", {}, [det]);
  }
  function renderRuns(runs) {
    limpar(elRunsBody);
    if (!runs || runs.length === 0) {
      elRunsBody.appendChild(el("tr", {}, [el("td", { colspan: "7", "class": "vazio", text: "Nenhuma execução." })]));
      return;
    }
    runs.forEach(function (r) {
      var tr = el("tr");
      tr.appendChild(el("td", {}, [el("code", { text: r.correlationId || r.runId || "-" })]));
      tr.appendChild(el("td", { text: r.scenario || "-" }));
      tr.appendChild(el("td", { text: fmtData(r.createdAt) }));
      tr.appendChild(el("td", { "class": "num", text: typeof r.acceptedCount === "number" ? String(r.acceptedCount) : "-" }));
      tr.appendChild(el("td", { "class": "num", text: typeof r.failedCount === "number" ? String(r.failedCount) : "-" }));
      var cel = estadoCelula(r.observedState);
      if ((r.unverifiableTargets || []).length) {
        cel.appendChild(el("div", { "class": "instrucao", text: "não verificável: " + r.unverifiableTargets.join(", ") }));
      }
      tr.appendChild(cel);
      tr.appendChild(evidenciasCelula(r));
      elRunsBody.appendChild(tr);
    });
  }
  function valorOuTraco(v) { return (v === null || v === undefined) ? "—" : String(v); }
  function renderMetricas(m) {
    $("m-total").textContent = valorOuTraco(m.totalRuns);
    $("m-aceitas").textContent = valorOuTraco(m.acceptedEvents);
    $("m-falhas").textContent = valorOuTraco(m.failedEvents);
    $("m-dlq").textContent = (m.targetDlq && m.targetDlq.available) ? String(m.targetDlq.messages) : "—";

    var chips = $("m-estados");
    limpar(chips);
    var estados = Object.keys(m.observedStates || {});
    if (!estados.length) chips.appendChild(el("span", { "class": "vazio", text: "Nenhuma execução." }));
    estados.forEach(function (k) {
      chips.appendChild(el("span", { "class": "chip", text: (ROTULOS_ESTADO[k] || k) + ": " + m.observedStates[k] }));
    });

    var head = $("eb-head"), body = $("eb-body");
    limpar(head); limpar(body);
    var eb = m.eventbridge;
    $("m-eb-janela").textContent = eb && eb.windowHours ? "(últimas " + eb.windowHours + " h)" : "";
    if (!eb) {
      $("m-eb-aviso").textContent = "Métricas do EventBridge indisponíveis no modo demonstração.";
      return;
    }
    $("m-eb-aviso").textContent = eb.available
      ? "“—” = sem dados no CloudWatch na janela (não significa zero)."
      : "Não foi possível ler o CloudWatch (" + (eb.error || "erro") + "). Verifique a política da Lambda de leitura.";
    var tr = el("tr", {}, [el("th", { text: "Rule" })]);
    METRICAS_EB.forEach(function (n) { tr.appendChild(el("th", { text: n })); });
    head.appendChild(tr);
    Object.keys(eb.rules || {}).forEach(function (rule) {
      var row = el("tr", {}, [el("td", {}, [el("code", { text: rule })])]);
      METRICAS_EB.forEach(function (n) {
        var v = eb.rules[rule][n];
        row.appendChild(el("td", { "class": (v === null || v === undefined) ? "num sem-dado" : "num", text: valorOuTraco(v) }));
      });
      body.appendChild(row);
    });
  }
  function metricasDemo() {
    var aceitos = 0, falhas = 0, estados = {};
    demoRuns.forEach(function (r) {
      aceitos += r.acceptedCount; falhas += r.failedCount;
      estados[r.observedState] = (estados[r.observedState] || 0) + 1;
    });
    return { totalRuns: demoRuns.length, acceptedEvents: aceitos, failedEvents: falhas, observedStates: estados, eventbridge: null, targetDlq: null };
  }

  function carregar() {
    if (MODO_DEMO) { renderRuns(demoRuns); renderMetricas(metricasDemo()); return Promise.resolve(); }
    if (!perfilAtual()) { renderRuns([]); return Promise.resolve(); }
    return Promise.all([
      apiFetch("/runs").then(jsonOuErro),
      apiFetch("/metrics").then(jsonOuErro)
    ]).then(function (r) {
      renderRuns(r[0].runs || []);
      renderMetricas(r[1]);
    }).catch(function (err) {
      if (err.status === 403) setMsg(elStatus, "Acesso negado (HTTP 403): seu perfil não tem permissão de consulta.", "erro");
      else setMsg(elStatus, "Falha ao carregar dados: " + err.message, "erro");
    });
  }

  function dispararTeste() {
    if (MODO_DEMO) {
      setMsg(elStatus, "MODO DEMONSTRAÇÃO: nenhum evento real foi publicado. Configure a API em config.js.", "erro");
      return;
    }
    elBtn.disabled = true;
    setMsg(elStatus, "Publicando eventos...");
    apiFetch("/runs", { method: "POST", body: JSON.stringify({ scenario: elCenario.value }) })
      .then(jsonOuErro)
      .then(function (b) {
        setMsg(elStatus, "Aceite no bus: " + b.accepted + " evento(s), falhas: " + b.failed + ". correlationId=" + b.correlationId + ". Aguardando evidência dos consumidores.", "ok");
        return carregar();
      })
      .catch(function (err) {
        if (err.status === 403) setMsg(elStatus, "Acesso negado (HTTP 403): somente o perfil operador pode publicar.", "erro");
        else setMsg(elStatus, "Falha ao disparar: " + err.message, "erro");
      })
      .finally(atualizarUiAuth);
  }

  // ---------------------------------------------------------------------------
  // Inicializacao
  // ---------------------------------------------------------------------------
  if (MODO_DEMO && elBanner) elBanner.hidden = false;
  elBtn.addEventListener("click", dispararTeste);
  elLogin.addEventListener("click", iniciarLogin);
  elLogout.addEventListener("click", sair);

  (MODO_DEMO ? Promise.resolve() : tratarCallback()).then(function () {
    atualizarUiAuth();
    return carregar();
  });

  if (!MODO_DEMO) {
    if (cfg.REFRESH_INTERVAL_MS) {
      setInterval(function () { if (!document.hidden) carregar(); }, cfg.REFRESH_INTERVAL_MS);
    }
    setInterval(atualizarUiAuth, 30000);
  }
})();
