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
