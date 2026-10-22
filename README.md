# PC Screen — seu "Sunshine", só que leve e seu

Espelho de tela + áudio do PC pra TV, só pra ver (sem controle de volta — use o PS Remote Play / o
controle plugado no PC normalmente). Vídeo em MJPEG, áudio em MP3 contínuo: sem codec exótico, sem
WebRTC, sem servidor externo. Tudo num único `pc_screen_server.py`, com um painel web pra ajustar
tudo ao vivo.

**Por que MJPEG/MP3 e não H.264+WebRTC de verdade (como Sunshine/Moonlight):** essa TV é de 2019
(webOS 4, Chromium 53) e montar uma pipeline de baixa latência de verdade precisa de encode por
GPU, servidor de sinalização e um navegador que negocie tudo isso direito — muita coisa pra
depurar numa TV que eu nem consigo testar daqui. MJPEG e um MP3 contínuo não têm essa
complexidade: `<img src="...mjpg">` e `<audio src="...mp3">` decodificam sozinhos, nativamente,
desde sempre — sem JS de decode, sem buffer. A latência real é só o tempo do seu PC capturar +
comprimir + mandar.

**Não é mágica de graça**: você já está usando PS Remote Play (PS5 → PC), e isso aqui soma mais um
salto (PC → TV) em cima. Vídeo e áudio também são dois streams HTTP **independentes, sem relógio
compartilhado** — não tem sincronismo labial automático. O painel tem um ajuste manual de
defasagem pra compensar na mão, igual um soundbar.

## Rodar

No PC, só quando for usar (não fica em segundo plano sozinho):
```
cd pc-screen\server
python pc_screen_server.py
```
Abre `http://192.168.10.128:8090/` **no próprio PC** — é o painel de controle, com os links de
vídeo e áudio e os controles ao vivo. Teste os links lá antes de ir pra TV.

Primeira vez, o Windows pergunta se libera no firewall — libere para rede privada.

`Ctrl+C` pra parar. Não deixa nada rodando depois.

## Instalar o app na TV (uma vez só)

`app/config.js` já está com `pcHost: '192.168.10.128'` (o IP que apareceu nos seus testes de
rede). Se mudar, pegue o novo com `ipconfig` (Endereço IPv4) e troque ali. `audio: true` liga o
áudio junto; ponha `false` se quiser só a tela.

```
cd pc-screen\app
ares-package . -o build
ares-install -d lgtv build\com.cauan.pcscreen_1.0.0_all.ipk
ares-launch -d lgtv com.cauan.pcscreen
cd ..\..
```

## Painel de controle (`http://<pc>:8090/`)

Tudo nele aplica na hora, sem reiniciar o script nem reinstalar o app:
- **FPS**, **qualidade JPEG**, **escala** do vídeo.
- **Bitrate** e **buffer** do áudio — buffer maior é mais estável e mais atraso; menor é mais
  responsivo e mais risco de engasgo (o problema que você viu no Sunshine). Comece em 150ms e
  desce se quiser testar mais responsivo.
- **Defasagem manual** entre áudio e vídeo (ms, pode ser negativa). A TV lê esse valor assim que
  conecta.

Também dá pra mudar por linha de comando ao abrir:
```
python pc_screen_server.py --fps 24 --quality 65 --scale 0.85 --audio-bitrate 160 --audio-buffer-ms 200 --audio-delay-ms 120
```

## Segurança

Sem `--token`, **qualquer um na sua rede Wi-Fi que souber o IP** consegue abrir vídeo, áudio e até
mexer no painel enquanto o script roda — sem login. Pra travar:
```
python pc_screen_server.py --token "alguma-coisa-so-sua"
```
e bota o mesmo valor em `app/config.js` → `token`. Fora isso, rodar só "na mão" (sem ficar
residente) já limita bastante a janela de exposição.

## Sobre o engasgo de áudio (o mesmo problema do Sunshine)

Na primeira versão, cada conexão de áudio abria o microfone de loopback e pedia blocos grandes
(o `audio_buffer_ms` inteiro) direto nele. O Windows avisava "data discontinuity in recording" —
o buffer do sistema enchia e perdia pedaço entre um pedido e outro. Era exatamente o travamento
que você via no Sunshine.

A correção foi separar duas coisas que eu tinha misturado: **drenar o dispositivo de áudio**
(precisa ser frequente e em pedacinhos fixos de 20ms — isso não é mais configurável, de propósito)
e **quanto acumular antes de mandar pela rede** (isso sim é o `audio_buffer_ms` do painel). Agora
tem uma captura contínua numa thread própria, alimentando uma fila; se o consumidor atrasar, a
fila descarta os pedaços mais antigos em vez de crescer sem limite ou travar a captura — isso é
a diferença entre "atrasa um pouco" e "engasga".

## Testes

```
cd server
python3 -m unittest test_pc_screen_server -v
```
22 testes, contra captura de tela e de áudio falsas (não precisa de monitor nem microfone/alto-
falante real): framing MJPEG válido, frames decodificam como JPEG de verdade, MP3 com sync de
frame válido, `/config` atômico (um campo inválido não aplica os outros pela metade), CORS no
`/config` (sem isso a defasagem ajustável no painel não chegaria na TV), token aceito/recusado,
404, `--help` não quebra, e a fila de áudio: para de capturar quando o último cliente sai, e — o
teste que reproduz o bug acima — um produtor mais rápido que o consumidor descarta o áudio
antigo em vez de crescer sem limite.

**Não testado:** `ImageGrab`/`soundcard` de verdade no Windows, nem o app rodando na TV — isso só
você confirma.
