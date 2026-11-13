# CLAUDE.md — doc-sentinel

Lee y cumple **[AGENTS.md](./AGENTS.md)**. Este archivo solo añade notas de Claude Code.

- El router de intenciones y los comandos canónicos están en AGENTS.md (modos `clean`, `detect`, `update`).
- No instales MCP de Perlecta o subtext-scan «por tu cuenta» para saltarte `doc-sentinel`. Si el usuario ya tiene esos MCP, sigue usándolos solo como lectura auxiliar; el veredicto lo da `doc-sentinel`.
- No ejecutes Skill de watermarks-remover que dispare Layer B o el HTTP `/clean` de texto.
- Al añadir el proyecto: `git submodule update --init --recursive` y comprobar `doc-sentinel version`.
