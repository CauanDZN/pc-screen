// PC Screen: mostra o MJPEG de pc_screen_server.py em tela cheia e toca o /audio.mp3 junto.
// <img src="...mjpg"> e <audio src="...mp3"> decodificam os streams sozinhos, nativamente -- esse
// JS so troca as URLs quando um dos dois falha (servidor desligado ou ainda nao aberto) e tenta de
// novo. video e audio sao duas conexoes HTTP independentes, sem relogio em comum; audio_delay_ms
// (lido direto do servidor, ajustavel ao vivo no painel de controle) e so uma defasagem manual
// aplicada no momento em que cada um comeca a conectar -- nao um sincronismo continuo.
(function () {
  'use strict';

  var cfg = window.CONFIG || { pcHost: '', port: 8090, token: '', retryMs: 2000, audio: true };
  var feed = document.getElementById('feed');
  var audioFeed = document.getElementById('audioFeed');
  var status = document.getElementById('status');
  var statusText = document.getElementById('statusText');
  var audioRetryTimer = null;

  function baseUrl(path) {
    var qs = cfg.token ? ('?token=' + encodeURIComponent(cfg.token)) : '';
    qs += (qs ? '&' : '?') + 't=' + Date.now(); // cache-bust: a retry opens a fresh connection
    return 'http://' + cfg.pcHost + ':' + cfg.port + path + qs;
  }

  function showStatus(text) {
    statusText.textContent = text;
    status.classList.remove('hidden');
  }

  function hideStatus() {
    status.classList.add('hidden');
  }

  function connectVideo() {
    showStatus('Conectando no PC (' + cfg.pcHost + ')...');
    feed.src = baseUrl('/stream.mjpg');
  }

  function connectAudio() {
    if (!cfg.audio) return;
    audioFeed.src = baseUrl('/audio.mp3');
    var p = audioFeed.play();
    if (p && p.catch) p.catch(function () { /* autoplay recusado ou stream ainda nao pronto; o evento error cuida do retry */ });
  }

  feed.addEventListener('load', hideStatus);
  feed.addEventListener('error', function () {
    showStatus('PC nao respondeu. Confira se pc_screen_server.py esta rodando. Tentando de novo...');
    setTimeout(connectVideo, cfg.retryMs);
  });

  audioFeed.addEventListener('error', function () {
    clearTimeout(audioRetryTimer);
    audioRetryTimer = setTimeout(connectAudio, cfg.retryMs);
  });

  function start() {
    if (!cfg.pcHost) {
      showStatus('Falta configurar o IP do PC em config.js');
      return;
    }
    connectVideo();
    if (!cfg.audio) return;

    // le a defasagem atual no painel de controle do servidor antes de tocar o audio
    var url = 'http://' + cfg.pcHost + ':' + cfg.port + '/config' + (cfg.token ? '?token=' + encodeURIComponent(cfg.token) : '');
    var delayMs = 0;
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url, true);
    xhr.timeout = 3000;
    xhr.onload = function () {
      try { delayMs = JSON.parse(xhr.responseText).audio_delay_ms || 0; } catch (e) { /* usa 0 */ }
      schedule();
    };
    xhr.onerror = schedule;
    xhr.ontimeout = schedule;
    xhr.send();

    function schedule() {
      // video ja comecou a conectar la em cima (connectVideo()), sempre na hora -- a tela e a
      // prioridade, ela nao espera essa requisicao de /config terminar.
      // atraso positivo: audio espera `delayMs` depois disso. negativo: nao da pra "voltar no
      // tempo" o video que ja comecou, entao o audio so entra o quanto antes (defasagem negativa
      // vira, na pratica, "o mais cedo possivel" em vez de literalmente adiantado).
      setTimeout(connectAudio, Math.max(0, delayMs));
    }
  }

  start();
})();
