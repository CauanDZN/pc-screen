// PC Screen: configuracao. Edite e rode o install de novo (nao precisa recompactar mais nada).
window.CONFIG = {
  // IP local do PC que vai rodar pc_screen_server.py. Visto no ultimo teste de rede
  // (Test-NetConnection -> SourceAddress); confirme com `ipconfig` se a rede mudar.
  pcHost: '192.168.10.128',

  // Mesma porta passada em --port no servidor (padrao 8090 se voce nao passar nada).
  port: 8090,

  // So preencha se voce rodou o servidor com --token algumacoisa. Vazio = sem token.
  token: '',

  // Tentativas de reconexao enquanto o servidor nao estiver de pe.
  retryMs: 2000,

  // Toca o audio junto com o video? Desligue se so quiser a tela.
  audio: true
};
