/*
 * Configuração do dashboard.
 *
 * Este arquivo é PÚBLICO (servido pelo CloudFront). Nunca coloque aqui tokens,
 * senhas, client secret ou qualquer credencial. Os valores abaixo são apenas
 * identificadores públicos. O App Client do Cognito é público (sem segredo)
 * e a segurança vem do fluxo Authorization Code + PKCE.
 *
 * Substitua cada <VARIAVEL> pelo valor do seu ambiente (ver IMPLANTACAO.md,
 * seção "Variáveis usadas neste guia") ANTES de enviar o arquivo ao S3.
 * Valores que ainda contenham "<" são tratados como não configurados.
 *
 * Modo demonstração: ativo quando API_BASE_URL está vazio/não configurado OU
 * MODO_DEMONSTRACAO = true. Nesse modo só aparecem dados sintéticos rotulados,
 * nunca misturados a dados reais.
 */
window.LAB01_CONFIG = {
  // URL base da HTTP API, sem barra no final. Ex.: https://<API_ID>.execute-api.<REGIAO>.amazonaws.com
  API_BASE_URL: "<API_URL>",

  // Força o modo demonstração mesmo com a API configurada.
  MODO_DEMONSTRACAO: false,

  // Domínio do login gerenciado do Cognito, sem barra no final.
  COGNITO_DOMAIN: "<COGNITO_DOMAIN>",

  // Client ID do App Client SEM segredo (não confundir com o User Pool ID).
  COGNITO_CLIENT_ID: "<CLIENT_ID>",

  // Devem ser IGUAIS (inclusive a barra final) às URLs cadastradas no App Client.
  REDIRECT_URI: "<URL_DASHBOARD>/",
  LOGOUT_URI: "<URL_DASHBOARD>/",

  // Escopos OIDC solicitados no login.
  SCOPES: "openid email profile",

  // Intervalo de atualização automática da lista (ms).
  REFRESH_INTERVAL_MS: 8000
};
