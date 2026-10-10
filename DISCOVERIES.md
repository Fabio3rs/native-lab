# native-lab: descobertas do PoC

Este documento registra os experimentos que fundamentaram a implementação.
Valores variáveis como PIDs, portas e números de namespace foram omitidos.
Nenhuma chave privada gerada pelos testes deve ser copiada para documentação ou
versionada.

## Spikes existentes utilizados

- `native-lab-sshd-server.sh` foi inspecionado e reutilizado diretamente em um
  diretório temporário para validar o servidor SSH em modo inetd.
- `native-lab-ssh-smoke.sh` foi inspecionado e serviu de referência para os
  argumentos de `ssh`. O probe final usou a mesma composição, mas não executou
  esse arquivo integralmente.
- `invokebrwaptmux.sh` demonstrava o desenho anterior com tmux. Ele permanece
  apenas como histórico e não faz parte da implementação atual.
- Os arquivos em `run/` são artefatos dos spikes antigos. A implementação atual
  usa somente `$XDG_RUNTIME_DIR/native-lab`.

## Ambiente observado

Os comandos abaixo registram dependências e versões sem alterar o sistema:

```bash
for command in bash bwrap socat ssh sshd ssh-keygen flock realpath sha256sum ip; do
    command -v "$command"
done
bwrap --version
ssh -V
/usr/sbin/sshd -V
```

No host usado para o PoC foram observados bubblewrap 0.9.0 e OpenSSH 9.6p1.
A implementação não depende especificamente desses patch levels, mas depende
das opções de configuração usadas pelos scripts.

## Bind seletivo depois de um `/run` privado

O ponto a validar era se uma origem situada sob o `/run` do host ainda poderia
ser bindada depois de `--tmpfs /run`. O probe usou um diretório já existente
como origem somente para leitura:

```bash
bwrap \
  --unshare-user \
  --ro-bind / / \
  --tmpfs /run \
  --dir /run/native-lab-control \
  --bind /run/user/"$(id -u)"/pulse /run/native-lab-control \
  -- sh -c '
      find /run -mindepth 1 -maxdepth 2 -print
      test -e /run/native-lab-control/pid
  '
```

Resultado: o bind seletivo funcionou e o restante do `/run` do host não ficou
visível. Isso permite manter toda a sessão efêmera em `$XDG_RUNTIME_DIR` sem
expor esse diretório inteiro ao sandbox.

## Network namespace, loopback e capabilities

Dentro do sandbox de execução do Codex, operações netlink retornaram `EPERM`
por causa do seccomp externo. Esse resultado não era uma falha do bubblewrap ou
do desenho do native-lab. O probe foi repetido no host autorizado:

```bash
bwrap \
  --unshare-user \
  --unshare-pid \
  --unshare-ipc \
  --unshare-net \
  --unshare-uts \
  --new-session \
  --cap-drop ALL \
  --ro-bind / / \
  --tmpfs /tmp \
  --tmpfs /run \
  --proc /proc \
  --dev /dev \
  -- sh -c '
      id -u
      awk "/CapEff/ {print \$2}" /proc/self/status
      ip -brief addr show lo
      python3 -c "
import socket
s = socket.socket()
s.bind((\"127.0.0.1\", 0))
s.listen()
print(s.getsockname())
"
  '
```

Resultados:

- o processo continuou com o UID do usuário;
- `CapEff` foi `0000000000000000`;
- loopback recebeu `127.0.0.1/8` e `::1/128`;
- um listener pôde ser aberto em `127.0.0.1`.

O arquivo `operstate` de loopback mostrou `unknown`. Para este caso isso não
indica falha: endereço configurado e bind funcional são os testes relevantes.

## SSH sobre Unix socket

O servidor do spike foi copiado para um diretório de `mktemp`, montado dentro
do bubblewrap e iniciado com a mesma política de namespaces do PoC. A forma
essencial do servidor foi:

```bash
socat \
  'UNIX-LISTEN:/caminho/control/lab-ssh.sock,fork,mode=0600' \
  'EXEC:/caminho/control/sshd-inetd.sh'
```

O wrapper inetd executou:

```bash
exec /usr/sbin/sshd -i -e -f /caminho/control/sshd_config
```

O cliente foi conectado assim:

```bash
ssh \
  -T \
  -o BatchMode=yes \
  -o 'ProxyCommand=socat STDIO UNIX-CONNECT:/caminho/control/lab-ssh.sock' \
  -o HostKeyAlias=native-lab \
  -o UserKnownHostsFile=/caminho/control/known_hosts \
  -o StrictHostKeyChecking=yes \
  -o IdentitiesOnly=yes \
  -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no \
  -i /caminho/control/client_ed25519 \
  usuario@native-lab \
  'true'
```

Resultados confirmados no host:

- conexão pelo Unix socket;
- autenticação ED25519 com host key verificada;
- execução de comando remoto;
- processo remoto com capabilities efetivas zeradas;
- stdin, stdout, stderr, streaming e exit status preservados nos testes
  manuais e no smoke test do spike.

Não se deve adicionar a opção de endereço `stderr` ao `EXEC` do socat. Sem ela,
diagnósticos do `sshd -e` permanecem no log do daemon; com ela, poderiam
corromper o byte stream do protocolo SSH.

## Encerramento do holder

O comportamento do processo externo do bubblewrap foi verificado com:

```bash
bwrap \
  --unshare-user \
  --unshare-pid \
  --new-session \
  --cap-drop ALL \
  --ro-bind / / \
  --proc /proc \
  -- sh -c 'exec sleep 300' &
holder=$!

kill -TERM "$holder"
wait "$holder"
```

Ao enviar `SIGTERM` ao processo externo do bubblewrap, o filho no PID namespace
também terminou. Um teste posterior encontrou uma diferença importante:
`SIGKILL` não pode ser encaminhado e pode deixar o filho órfão.

Por isso, `native-labd` permanece como um supervisor mínimo e estável. O
bubblewrap é iniciado como seu filho com `--die-with-parent`. Assim:

- encerrar normalmente o holder encaminha `SIGTERM` ao bubblewrap;
- matar abruptamente o holder também mata o bubblewrap e o sandbox;
- o bubblewrap não depende do processo `native-lab`/`ssh`, que pode terminar
  sem encerrar a sessão.

Antes de sinalizar o supervisor, a implementação valida PID, start-time de
`/proc`, caminho de `native-labd` e diretório da sessão nos argumentos do
processo, evitando atingir um PID reutilizado.

## Adapter MCP e espera por output

A `.venv` do PoC foi validada com `mcp==2.2.0`. O SDK high-level registra as
tools estruturadas e o transporte stdio protege o FD do protocolo, mas a
extensão Tasks de 2026 ainda consta como não implementada no roadmap oficial.
Os símbolos `Task*` presentes em `mcp-types` são modelos *types-only* das Tasks
core de 2025 e não registram handlers como `tasks/get` no servidor.

Por isso `expect` começa como uma chamada bloqueante com timeout. Seu núcleo é
um `asyncio.Condition` sobre o ring buffer, independente do MCP, e pode ser
envolvido por uma implementação futura de Tasks sem mudar a semântica. O
matcher literal foi verificado com o padrão dividido entre duas leituras do
pipe e com retomada por cursor dentro de um único evento.

O smoke ponta a ponta usou o cliente oficial sobre stdio para iniciar:

```text
sh -c 'sleep 0.3; printf "ready in 183 ms\n"; sleep 30'
```

`run` devolveu imediatamente um `process_id`; `expect("ready in")` encontrou a
linha em stdout; `tail` devolveu o mesmo evento; e `kill` sinalizou o grupo do
cliente SSH. O workload foi executado pelo `native-lab run -- ...`, não
diretamente no host.

## Reprodução consolidada

O teste automatizado reúne as verificações funcionais:

```bash
./native-lab-smoke.sh
```

Ele cria seu próprio `XDG_RUNTIME_DIR` em `/tmp`, testa concorrência, isolamento,
localhost interno, ausência de Internet, stdio, streaming, quoting, exit status,
PATH para ferramentas como `npm`, recuperação stale e stop. Todo o runtime
temporário é removido ao final.

## Filesystem allowlist (policy v2)

A raiz vazia foi validada substituindo o bind da raiz do host por uma sequência
conceitualmente equivalente a:

```bash
bwrap \
  --unshare-user \
  --unshare-pid \
  --unshare-net \
  --tmpfs / \
  --dev /dev \
  --ro-bind /usr /usr \
  --ro-bind /etc /etc \
  --ro-bind /bin /bin \
  --tmpfs /tmp \
  --tmpfs /run \
  --proc /proc \
  -- sh -c 'test ! -e /var; command -v sshd; command -v python3'
```

Na implementação, os roots de sistema existentes são adicionados
individualmente e o root final é remountado read-only. O workspace é bindado
RW, HOME, `/tmp`, `/run` e `/dev/shm` são tmpfs privados, e somente o diretório
de controle atravessa o `/run` privado.

O teste consolidado que comprovou a policy é:

```bash
timeout 180 ./native-lab-policy-smoke.sh
```

Ele cria, sem consultar o config real da máquina:

```text
current/       workspace RW
trusted/       entrada trust_level="trusted"
untrusted/     entrada trust_level="untrusted"
extra-ro/      configuração NativeLab explícita
config.toml    configs Codex e NativeLab temporários
```

Foram confirmados:

- arquivos normais, `.env` e `.pem` do workspace atual acessíveis e RW;
- `.git`, `.agents` e `.codex` do workspace inacessíveis;
- projeto trusted RO e projeto untrusted ausente;
- `.env*`, chaves/certificados e metadata do trusted negados;
- `~/.npm-global` executável mas RO e `~/.npm` legível mas RO;
- HOME real, browser profiles, host runtime, D-Bus e host `/dev/shm` ausentes;
- mudança de trust ou de `extra_read_only` alterando digest e session id;
- executável `rg` falso no PATH do workspace não usado pelo launcher.

## TOML do Codex observado

O import usa a tabela top-level `projects` do config global do Codex:

```toml
[projects."/caminho/absoluto"]
trust_level = "trusted"
```

O parser é `tomllib` da stdlib. Tabelas e opções do Codex não relacionadas são
ignoradas; somente `projects.<path>.trust_level == "trusted"` concede o mount
RO. O config NativeLab é propositalmente mais estrito e rejeita chaves
desconhecidas, tipos errados, symlink, owner diferente ou modo gravável por
group/others.

O manifest pode ser inspecionado sem iniciar bubblewrap:

```bash
runtime=$(mktemp -d /tmp/native-lab-policy.XXXXXX)
chmod 700 "$runtime"
./native-lab-policy.py resolve \
  --workspace "$PWD" \
  --rg /usr/bin/rg \
  --runtime-root "$runtime" \
  --output "$runtime/policy.json"
python3 -m json.tool "$runtime/policy.json"
```

O stdout do `resolve` é o SHA-256 da representação canônica sem o próprio
campo digest. Nenhum TOML é transformado em shell e não há `eval`.

## Masks de arquivos

A primeira tentativa de mascarar cada arquivo com `--ro-bind-data` reutilizou
um único file descriptor. Bubblewrap 0.9.0 fecha o FD após consumi-lo, então o
segundo mask falhou com file descriptor inválido. A implementação final cria
um único arquivo regular mode `000` no session dir e usa `--ro-bind` dele para
cada destino de arquivo. Diretórios usam tmpfs mode `000` e read-only.

As identidades `st_dev`/`st_ino` de paths protegidos existentes são registradas
no manifest e verificadas novamente pelo holder antes de criar o sandbox.
Symlinks e objetos não regulares/não diretórios falham fechado.

## Por que os mountpoints sintéticos persistem

Para negar um `.git` que ainda não existe dentro de um workspace bindado RW,
bubblewrap precisa de um mountpoint nesse workspace. Foi testada uma estratégia
de remover o diretório host assim que o mount estivesse pronto. Ela não é
segura: depois da remoção, um comando dentro da sessão conseguiu recriar o nome
através do bind RW; num probe, `touch .codex` produziu um arquivo persistente no
host. Um monitor por polling apenas reduzia a corrida e não constituía DENY.

A decisão do PoC passou a ser manter o mountpoint vazio durante a sessão. O
probe usado seguiu esta forma:

```bash
native-lab run sh -c '
  for path in .git .agents .codex; do
    test -d "$path"
    test ! -r "$path"
    ! touch "$path/probe" 2>/dev/null
  done
'

native-lab status
native-lab run true
native-lab stop
```

No host, os diretórios existiam vazios enquanto `status` mostrava `alive`; após
`stop`, somente os que tinham sido criados pelo NativeLab desapareceram. O
registro de ownership usa PID + start-time de `/proc`, inode do target e um
marker por session id. Um lock global serializa resolução, registro e cleanup.

Esse comportamento é deliberadamente visível e está documentado no README.
Ele evita a corrida causada pelo processo sandboxed, mas não pretende se
defender de outro processo host-side malicioso executando como o mesmo usuário.

## Execução dos testes a partir do Codex

Uma aprovação de comando significa que o launcher e o smoke test rodam no host.
Isso é necessário porque o sandbox externo do Codex pode bloquear a criação de
network namespaces. O comando aprovado inicia `native-lab`, e **só então** os
comandos passados a `native-lab run` executam no bubblewrap interno.

Nesta rodada foram executados no host:

```bash
timeout 180 ./native-lab-policy-smoke.sh
timeout 180 ./native-lab-smoke.sh
```

Ambos terminaram com todos os testes passando. O tempo mostrado antes do clique
de aprovação era espera da interface; a execução começou após o usuário
autorizar e terminou imediatamente/rapidamente.


## GPU AMD por workspace: revisão e experimentos (2026-10-10)

A revisão anterior à implementação confirmou: configuração validada no host em
`parse_native_config`/`checked_config_path`; manifest canônico e SHA-256 em
`compile_policy`; argumentos NUL em `emit_bwrap`; `--args` antes de `--clearenv`
no daemon; resolução antes do session ID; `stop` procurava somente a policy
corrente; `encode_remote_command` reconstruía o ambiente SSH. Não houve diferença
material desses pontos em relação à proposta. Nenhuma variável GPU foi necessária;
assim, não foi introduzido encaminhamento de ambiente nem comandos de configuração.
Policy 2/manifest 1 passaram a 3/2. Config 1 continua válido sem GPU, config 2
adiciona autorização explícita por caminho canônico. Manifests antigos servem
somente para inspeção/revogação; não inicializam novas sessões.

Host: Radeon RX 7900 XTX, PCI `0000:0b:00.0`, vendor `0x1002`, device `0x744c`,
driver `amdgpu` confirmado por sysfs. Render node naquele boot:
`/dev/dri/renderD128` (226:128); KFD (235:0). A associação é descoberta, não fixa.
UID/GID 1000 no host, grupos render 109/video 44; render node com ACL individual
rw, KFD root:render 0660. Nenhuma permissão ou membership foi alterada.
Na sessão SSH, UID/GID 1000, mappings `1000 0 1`; vários grupos aparecem como
65534, mas o acesso efetivo permaneceu válido. `os.open(O_RDWR)` e ioctls reais
funcionaram. Essa observação não garante permissões em outras instalações;
o launcher revalida também depois do SSH e recusa workloads se perder acesso.

Mesa/Vulkan em `/usr`, `/lib` e `/etc` já estavam cobertos pelos mounts existentes,
incluindo ICDs, EGL vendor manifests e drirc. Loader, ICD e implementação são
componentes distintos: um loader presente não prova driver utilizável.
Neste host há symlinks de drivers DRI em `/usr/lib` para `/opt/amdgpu` (Mesa
25.0.0-devel); RADV do sistema é Mesa 25.2.8. ROCm está em `/opt/rocm-6.4.2`,
sem `/opt/rocm` nem `/opt/rocm/core`; existe também instalação antiga versionada.
Os testes autorizaram explicitamente os dois roots necessários, sem compartilhar
`/opt` inteiro ou escolher automaticamente uma instalação. Runtime no workspace
continua permitido. Instalação e compatibilidade do SDK são pré-requisitos externos.

Sysfs inclui links cruzados entre class/drm, dev/char, bus/pci e devices, além de
KFD topology e CPU/NUMA. Traces de vulkaninfo, EGL surfaceless e rocminfo consultaram
essas árvores, sem acessos a `/run/udev/data`. A implementação inicial monta `/sys`
completo read-only somente com GPU ativa; isso revela metadados de outros dispositivos
e CPUs, mas não concede seus device nodes. Uma árvore seletiva exigiria reproduzir
os links e topologia observados: complexidade adiada. Nenhum mount de udev ou `/run`
foi adicionado. Os devices são individuais, sem card*, framebuffer ou display host.

Diagnósticos diretos no host confirmaram Vulkan AMD, EGL surfaceless e ROCm.
`eglinfo` geral retornou 2 por plataformas indisponíveis; o teste específico
`eglinfo -B -p surfaceless` passou. Dentro do NativeLab, a suíte de hardware confirmou
Vulkan Radeon, EGL AMD, rocminfo e kernel HIP: alocação, dispatch, sincronização,
cópia e validação de 1024 valores em gfx1100. HIP foi compilado e executado dentro
da sessão. GPU desligada: sem devices ou sysfs; graphics: sem KFD; namespaces,
capabilities, rede privada e ausência de sockets X11/Wayland do host preservados.

Xvfb 21.1.12: `glxinfo -B` reportou direct rendering Yes, mas Accelerated no e
renderer llvmpipe. `glxgears` abriu/renderizou por software até o timeout esperado.
`vkcube` selecionou Radeon RX 7900 XTX, mas saiu 1: `No DRI3 support detected`
e `Could not find both graphics and present queues`. Portanto, acesso à GPU e
apresentação numa janela X11 são requisitos diferentes. Xvfb fornece framebuffer
em memória; não passa a oferecer DRI3 ao receber um render node. Nenhuma extensão
ou variável foi forçada para mascarar esse resultado. Outro backend de display
isolado ainda precisa ser escolhido e validado para janelas aceleradas.

Lifecycle: sessões de digests diferentes são enumeradas por workspace exato.
`stop --all` usa manifests salvos, PID/start-time e argv pareado, mesmo com TOML
inválido. Um teste inicialmente comparava `status` recalculado de outro workspace:
a remoção de placeholders de projetos trusted mudava seu digest, embora o holder
original continuasse vivo. A asserção passou a validar a identidade salva original.
Mocks cobrem ausência de hardware, paths arbitrários, symlinks, devices regulares,
permissões, vendor/driver/sysfs, substituição do node, digest e holders não relacionados.
CI não declara teste de hardware aprovado quando há apenas mocks: ausência retorna
77 e é registrada como NOT RUN. O smoke de hardware exige seleção explícita.

Referências: [DRM render nodes](https://docs.kernel.org/gpu/drm-uapi.html#render-nodes),
[ROCm device passthrough](https://rocmdocs.amd.com/en/latest/install/docker-containers.html),
[Xvfb](https://xorg.freedesktop.org/releases/X11R6.9.0/doc/html/Xvfb.1.html),
[NVIDIA CDI](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html).
A extensão futura NVIDIA terá provider próprio; não há CUDA/backend fictício,
leitura de CDI do workspace ou reprodução de opções Docker que relaxem o isolamento.

Comparação solicitada fora do NativeLab: `xvfb-run -a -s '-screen 0 640x480x24 -nolisten tcp'` no host produziu exatamente o mesmo resultado. `xdpyinfo` anunciou GLX e Present, sem DRI3; `glxinfo` retornou llvmpipe/Accelerated no; `glxgears` executou até timeout de 5 segundos (124); `vkcube` selecionou a Radeon e saiu 1 por ausência de DRI3. O servidor temporário foi encerrado pelo xvfb-run. Isso localiza a limitação nesse caminho Xvfb, independentemente do sandbox.


## MCP e instalação duplicada: identidade do holder

Após instalar a CLI em `~/.local/bin`, o MCP ainda selecionava por padrão o
launcher do checkout. O ID de sessão depende do workspace/profile/policy, mas a
validação de argv exige o caminho exato do daemon. O usuário confirmou que
`NATIVE_LAB_BIN=/home/fabio/.local/bin/native-lab` no MCP resolveu a falha
`session is operational but holder identity is invalid`. A correção alinha os
clientes sem remover a checagem de identidade e sem encerrar a sessão válida.

O shell desta investigação e o host tinham namespaces PID diferentes, mas isso
não prova que o MCP tinha a mesma limitação. Não havia uma sessão ativa do projeto
para inspecionar quando o diagnóstico foi iniciado. A nova ferramenta MCP
`diagnostics` reporta seu próprio contexto e caminhos, sem revelar todo o ambiente.
O holder-check distingue PID invisível/encerrado, permissão em proc, start-time,
metadata e argv; `--diagnose` fornece JSON. Invisibilidade não é diagnosticada
como morte comprovada. As regras de sinalização e reutilização continuam iguais.
`stop` repetido agora informa sessão já encerrada; resíduos recuperados recebem
uma mensagem diferente de encerramento de holder ativo.
