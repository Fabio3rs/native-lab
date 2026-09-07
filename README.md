# native-lab

> [!CAUTION]
> **Este projeto é somente uma prova de conceito experimental e potencialmente
> perigosa. Ele não foi auditado e não deve ser tratado como uma fronteira de
> segurança para executar código hostil, malware, instaladores desconhecidos ou
> dependências não confiáveis.**

`native-lab` mantém uma sessão bubblewrap persistente por workspace. Processos
iniciados em chamadas diferentes compartilham os mesmos namespaces e, portanto,
o mesmo localhost privado:

```text
native-lab run npm run dev
                    │
                    │ mesma sessão / mesmo localhost
                    ▼
native-lab run curl http://127.0.0.1:5173
```

Comandos posteriores entram na sessão por SSH sobre um Unix socket. O SSH é
usado deliberadamente como transporte de execução, stdin, stdout, stderr e exit
status; não existe um protocolo próprio de daemon ou multiplexação de pipes.

## Leia isto antes de executar

O objetivo atual é validar arquitetura e comportamento, não oferecer isolamento
forte contra um atacante. As limitações mais importantes são:

### O filesystem do host continua legível

O sandbox começa com:

```text
--ro-bind / /
```

Isso impede escrita na maior parte da raiz, mas **não esconde arquivos**. Todo
arquivo que o usuário atual consegue ler no host normalmente continua legível
dentro da sessão, incluindo potencialmente:

- chaves e configurações em `$HOME`;
- tokens de CLIs e credenciais de cloud;
- código-fonte fora do workspace;
- configurações do Git, npm e outras ferramentas;
- informações expostas por `/sys` e outras árvores read-only.

Read-only não protege confidencialidade. Um comando malicioso poderia ler um
segredo e escrevê-lo no workspace, imprimi-lo no terminal ou deixá-lo preparado
para exfiltração posterior. A ausência de Internet reduz alguns caminhos de
exfiltração, mas não torna esse desenho seguro para código hostil.

### O workspace é totalmente gravável

O diretório retornado por `realpath "$PWD"` é rebindado read-write. Qualquer
processo da sessão pode criar, alterar, truncar, renomear ou apagar arquivos que
o usuário conseguir modificar nesse workspace.

Execute `native-lab` a partir do diretório exato que deseja tornar gravável. Se
ele for iniciado em um diretório amplo, como o próprio `$HOME`, todo esse
diretório será considerado workspace e ficará gravável no sandbox.

Workspaces em `/`, `/run`, `/tmp`, `/proc` e `/dev` são rejeitados porque
conflitariam com os mounts privados essenciais, mas essa validação não impede o
usuário de escolher outros diretórios excessivamente amplos.

Use controle de versão, backups e dados descartáveis.

### Isto compartilha o kernel do host

Bubblewrap usa namespaces; não é uma máquina virtual. Os processos continuam
usando o mesmo kernel do host. A policy atual:

- cria user, PID, IPC, network e UTS namespaces;
- remove todas as capabilities efetivas com `--cap-drop ALL`;
- não instala uma política seccomp própria;
- não instala uma política Landlock, SELinux ou AppArmor própria;
- não oferece proteção contra vulnerabilidades do kernel.

Remover capabilities é uma camada útil, mas não transforma o PoC em isolamento
adequado para código adversarial.

### Não existem limites de recursos

Não há cgroups, limites de CPU ou memória, quota de disco, limite de processos
ou supervisor resistente a fork bombs. Um processo pode consumir recursos do
usuário ou do host até os limites externos já existentes.

### O canal de controle é compartilhado e gravável

O único diretório deliberadamente compartilhado entre host e sandbox contém o
socket SSH e suas chaves efêmeras. Ele é gravável pela sessão. Um processo
malicioso dentro do sandbox pode causar denial of service, remover o socket,
alterar credenciais da sessão ou tentar se passar pelo listener para execuções
posteriores.

As permissões `0700`/`0600`, autenticação por chave e known-hosts dedicado
protegem contra outros usuários do host em condições normais. Elas não protegem
o canal contra código já executando dentro da própria sessão.

### O código é um PoC em shell

O lifecycle possui flock, probe funcional por SSH e validação de PID,
start-time e argv antes de enviar sinais. Mesmo assim, não há pidfd, daemon de
sistema, journal estruturado, atualização transacional ou auditoria de
segurança. Corridas e casos de falha ainda podem existir.

## O que o PoC isola

Com a policy version 1, o layout conceitual é:

| Recurso | Comportamento |
| --- | --- |
| `/` | Bind recursivo read-only do host |
| Workspace | Bind read-write no mesmo caminho absoluto |
| `/tmp` | tmpfs privado |
| `/run` | tmpfs privado |
| `/run/native-lab-control` | Único bind de controle compartilhado |
| `/proc` | procfs do novo PID namespace |
| `/dev` | Dispositivos mínimos criados pelo bubblewrap |
| Rede | Network namespace privado, apenas loopback |
| Capabilities | Todas removidas |

Consequências verificadas pelo smoke test:

- processos de chamadas diferentes alcançam uns aos outros em `127.0.0.1`;
- o localhost do host não aparece dentro da sessão;
- serviços da sessão não aparecem no localhost do host;
- a sessão não alcança a Internet;
- o bus D-Bus, SSH agent, X11 e demais sockets do `$XDG_RUNTIME_DIR` do host não
  são montados;
- caminhos fora do workspace permanecem read-only dentro do possível.

Isso descreve o host em que o PoC foi testado. Configuração do kernel,
bubblewrap, LSMs e namespaces pode variar entre distribuições. Falhar ao criar
qualquer propriedade essencial encerra o startup; não há fallback para execução
no host.

## Requisitos

- Linux com user namespaces habilitados;
- Bash;
- bubblewrap (`bwrap`);
- socat;
- cliente e servidor OpenSSH (`ssh`, `sshd`, `ssh-keygen`);
- `flock`, `realpath`, `sha256sum`, `stat`, `awk` e ferramentas Unix usuais;
- `$XDG_RUNTIME_DIR` válido.

`$XDG_RUNTIME_DIR` precisa ser absoluto, existir, pertencer ao usuário atual,
ter modo `0700` e permitir escrita e travessia. O programa falha explicitamente
caso isso não seja verdade.

Os executáveis `native-lab`, `native-labd` e `native-lab-session` devem
permanecer juntos. `native-lab` resolve seu próprio caminho real para localizar
os outros dois.

## Uso

Execute a partir do workspace desejado:

```bash
./native-lab run COMMAND [ARGS...]
./native-lab status
./native-lab stop
```

Exemplo:

```bash
./native-lab run npm run dev
```

Em outro terminal, no mesmo diretório canônico:

```bash
./native-lab run curl http://127.0.0.1:5173
```

Também é aceito `--` antes do comando:

```bash
./native-lab run -- sh -c 'exit 37'
echo "$?"
# 37
```

O workspace é exatamente o `$PWD` canônico. Rodar a CLI em dois subdiretórios
diferentes cria duas sessões diferentes, mesmo que ambos pertençam ao mesmo
repositório. A opção `--workspace` ainda não existe.

### Semântica de `run`

O modo padrão usa SSH sem PTY (`ssh -T`):

```text
stdin  do caller -> comando
stdout do comando -> stdout do caller
stderr do comando -> stderr do caller
exit do comando -> exit do native-lab/ssh
```

Programas que exigem terminal interativo, TTY, interface full-screen ou prompt
de senha não fazem parte desta etapa.

Os argumentos são transportados por quoting POSIX, inclusive valores vazios,
espaços, aspas e quebras de linha. O PATH do caller é aplicado à resolução do
comando remoto. As demais variáveis de ambiente do caller não são encaminhadas
automaticamente.

### Limitações para npm e npx

O PATH é preservado para que instalações do usuário, como
`$HOME/.npm-global/bin/npm`, possam ser encontradas. Entretanto:

- o `$HOME` do host permanece read-only;
- a Internet está desabilitada;
- `npx -y` não conseguirá baixar pacotes ausentes;
- ferramentas que precisam escrever caches em `$HOME` podem falhar.

Para este PoC, dependências devem estar no workspace ou já disponíveis de forma
compatível com essas restrições. Montagem seletiva de caches ou um HOME privado
gravável é trabalho futuro e precisa ser avaliada como mudança de policy.

## Identificação e runtime

A sessão é identificada por SHA-256 de:

```text
realpath($PWD) + NUL + profile + NUL + policy_version
```

São usados os primeiros 24 caracteres. Atualmente:

```text
profile=default
policy_version=1
```

O layout é:

```text
$XDG_RUNTIME_DIR/native-lab/<session-id>/
├── start.lock
├── holder.pid
├── session.info
├── daemon.log
└── control/
    ├── lab-ssh.sock
    ├── client_ed25519
    ├── client_ed25519.pub
    ├── ssh_host_ed25519_key
    ├── ssh_host_ed25519_key.pub
    ├── authorized_keys
    ├── known_hosts
    ├── sshd_config
    ├── sshd-inetd.sh
    └── user
```

Diretórios usam modo `0700`; socket, chaves privadas e arquivos sensíveis usam
`0600`. O comprimento do socket é validado contra o limite Linux de 107 bytes.

As chaves são efêmeras e desaparecem quando o runtime do usuário é limpo. Elas
não devem ser copiadas, compartilhadas ou versionadas.

## Lifecycle

### Startup

1. O cliente calcula a sessão pelo workspace.
2. Um probe SSH verifica se ela já está operacional.
3. `flock` serializa startups concorrentes.
4. O probe é repetido dentro do lock.
5. Estado stale é removido e `native-labd` é iniciado com `nohup`.
6. O cliente espera até cinco segundos pelo SSH funcional.
7. Com a sessão pronta, o cliente libera o lock e faz `exec ssh`.

O PID sozinho não declara prontidão. A autoridade é uma autenticação SSH real
executando `true`.

### Holder

`native-labd` é um supervisor mínimo. Ele inicia o bubblewrap como filho com
`--die-with-parent` e encaminha encerramento normal. Isso evita deixar o
sandbox órfão quando o holder sofre `SIGKILL`, sem atrelar sua vida ao cliente
`ssh` que iniciou a sessão.

### Stop

`native-lab stop`:

1. adquire o mesmo lock usado pelo startup;
2. valida PID, start-time de `/proc`, caminho do daemon, workspace e sessão;
3. envia `SIGTERM`;
4. espera dois segundos;
5. usa `SIGKILL` se necessário;
6. remove socket, chaves e metadados stale.

O lock e o último `daemon.log` são preservados. Nunca é executado um
`kill "$(cat holder.pid)"` sem validação.

## SSH interno

O listener persistente é conceitualmente:

```text
caller
  ↕
ssh -T
  ↕
socat / Unix socket
  ↕
sshd -i
  ↕
COMMAND
```

O sshd aceita somente a chave pública da sessão. Password, PAM, TTY, X11,
agent forwarding, TCP forwarding, Unix forwarding, tunnels, user environment e
user RC estão desabilitados.

O socat não usa sua opção `stderr` no endereço `EXEC`. Isso é intencional:
diagnósticos do sshd precisam permanecer no log, fora do byte stream SSH.

## Testes

O smoke test precisa ser executado num host que permita user e network
namespaces. Sandboxes externos com seccomp podem bloquear netlink ou criação de
sockets e produzir `EPERM` antes que o PoC seja iniciado.

```bash
./native-lab-smoke.sh
```

O teste usa um `$XDG_RUNTIME_DIR` temporário sob `/tmp` e valida:

- startup concorrente;
- permissões de runtime, socket e chaves;
- namespaces e capabilities;
- fronteira read-only e workspace read-write;
- isolamento do `/run`;
- localhost interno e separação do localhost do host;
- ausência de Internet;
- streaming, stdin, stdout e stderr;
- exit status e quoting de argv;
- PATH para npm;
- recuperação após morte abrupta;
- `status` e `stop` idempotente.

O runtime temporário é removido ao final.

### GitHub Actions

O workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml) executa em um
runner GitHub-hosted `ubuntu-24.04` para pushes, pull requests e disparos
manuais. Ele:

- instala bubblewrap, socat, OpenSSH, ShellCheck e as demais dependências;
- valida os modos executáveis e a sintaxe Bash;
- executa ShellCheck;
- impede que `run/` ou chaves privadas OpenSSH sejam versionados;
- executa o smoke test completo com timeout de 120 segundos.

Ubuntu 24.04 pode restringir user namespaces sem privilégio por AppArmor. Como
o runner GitHub-hosted é uma VM descartável, o workflow desativa essa restrição
externa somente durante o job. Isso permite testar o bubblewrap como o usuário
normal do runner; não significa que o native-lab contorne essa política num host
real, nem é uma recomendação para desativá-la permanentemente em uma máquina de
desenvolvimento.

O job inteiro possui timeout de dez minutos e permissões GitHub limitadas a
leitura do conteúdo do repositório.

## Diagnóstico

Confira o estado no mesmo workspace:

```bash
./native-lab status
```

Em falha de startup, o cliente mostra até as primeiras 200 linhas de
`daemon.log`. O caminho completo do runtime também aparece em `status`.

Erros comuns:

- `XDG_RUNTIME_DIR is not set`: a variável não está disponível na sessão atual;
- `session failed to become ready`: consulte o `daemon.log` exibido;
- `missing dependency`: instale a ferramenta indicada;
- caminho do socket longo demais: use um `$XDG_RUNTIME_DIR` mais curto;
- `Operation not permitted`: user namespaces, net namespaces, LSM ou seccomp do
  ambiente externo bloquearam o bubblewrap;
- `Read-only file system`: a ferramenta tentou escrever fora do workspace,
  `/tmp` ou `/run/native-lab-control`.

## Fora de escopo

Esta versão não implementa:

- protocolo próprio de execução;
- tmux;
- cgroups ou quotas;
- pidfd;
- seccomp customizado;
- proxy MCP;
- Xvfb, Wayland ou acesso a display do host;
- integração com Docker;
- código C/C++;
- profiles configuráveis de capabilities.

GDB/ptrace exigirá uma policy futura específica. Dependendo do alvo, UID, Yama
e seccomp, ptrace pode funcionar sem `CAP_SYS_PTRACE` ou exigir permissões
adicionais. Qualquer alteração deve criar uma nova `policy_version`, evitando
reutilizar uma sessão criada sob regras antigas.

## Documentação dos experimentos

Os comandos utilizados para validar mounts, loopback, capabilities, SSH sobre
UDS e encerramento do holder estão registrados em [DISCOVERIES.md](DISCOVERIES.md).

## Estado do projeto

**PoC experimental. Não usar em produção e não executar código não confiável.**

Antes de evoluir para uma ferramenta de segurança seria necessário definir um
threat model, ocultar segredos do host em vez de apenas torná-los read-only,
adicionar políticas de syscall e recursos, endurecer o canal de controle e
submeter a implementação a revisão especializada.
