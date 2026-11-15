"""Aviso por e-mail (SMTP) ao fim de cada execução do serviço de prospecção.

Tudo vem do .env, nunca do código:

    SMTP_HOST=smtp.gmail.com          # vazio = sem e-mail (só log)
    SMTP_PORTA=587
    SMTP_SEGURANCA=starttls           # starttls | ssl | nenhuma
    SMTP_USUARIO=conta@dominio
    SMTP_SENHA=...                    # Gmail: senha de app (com verificação em 2 etapas)
    EMAIL_DE=                         # vazio = SMTP_USUARIO
    EMAIL_PARA=voce@dominio           # vários separados por vírgula

Falha de e-mail nunca derruba a execução: vai para o log e a função devolve False.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

logger = logging.getLogger(__name__)

SEGURANCAS = ("starttls", "ssl", "nenhuma")


@dataclass
class ConfigEmail:
    host: str
    porta: int
    seguranca: str
    usuario: str
    senha: str
    remetente: str
    destinatarios: list[str]
    timeout: int = 60

    @classmethod
    def from_env(cls) -> ConfigEmail | None:
        """None quando o e-mail não está configurado (SMTP_HOST ou EMAIL_PARA vazios)."""
        host = os.getenv("SMTP_HOST", "").strip()
        destinatarios = [e.strip() for e in os.getenv("EMAIL_PARA", "").split(",") if e.strip()]
        if not host or not destinatarios:
            return None
        seguranca = os.getenv("SMTP_SEGURANCA", "starttls").strip().lower()
        if seguranca not in SEGURANCAS:
            raise ValueError(f"SMTP_SEGURANCA deve ser {', '.join(SEGURANCAS)} (veio {seguranca!r})")
        usuario = os.getenv("SMTP_USUARIO", "").strip()
        remetente = os.getenv("EMAIL_DE", "").strip() or usuario
        if not remetente:
            raise ValueError("Defina EMAIL_DE ou SMTP_USUARIO no .env")
        return cls(
            host=host,
            porta=int(os.getenv("SMTP_PORTA", "465" if seguranca == "ssl" else "587")),
            seguranca=seguranca,
            usuario=usuario,
            senha=os.getenv("SMTP_SENHA", ""),
            remetente=remetente,
            destinatarios=destinatarios,
        )


def enviar_email(
    assunto: str,
    corpo: str,
    cfg: ConfigEmail | None = None,
    smtp_factory: Callable[..., Any] | None = None,
) -> bool:
    """Envia um e-mail de texto. True se enviou; False se não configurado ou se falhou."""
    try:
        cfg = cfg or ConfigEmail.from_env()
    except ValueError as e:
        logger.error(f"E-mail não enviado — configuração inválida: {e}")
        return False
    if cfg is None:
        logger.info("E-mail não configurado (SMTP_HOST/EMAIL_PARA vazios) — aviso só no log")
        return False

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = cfg.remetente
    msg["To"] = ", ".join(cfg.destinatarios)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.remetente.rpartition("@")[2] or None)
    msg.set_content(corpo)

    try:
        if smtp_factory is None:
            smtp_factory = smtplib.SMTP_SSL if cfg.seguranca == "ssl" else smtplib.SMTP
        kwargs: dict[str, Any] = {"timeout": cfg.timeout}
        if cfg.seguranca == "ssl":
            kwargs["context"] = ssl.create_default_context()
        with smtp_factory(cfg.host, cfg.porta, **kwargs) as smtp:
            if cfg.seguranca == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if cfg.usuario:
                smtp.login(cfg.usuario, cfg.senha)
            smtp.send_message(msg)
    except Exception as e:  # rede, autenticação, TLS: nunca derruba a execução
        logger.error(f"E-mail não enviado ({cfg.host}:{cfg.porta}): {e}")
        return False
    logger.info(f"E-mail enviado para {', '.join(cfg.destinatarios)}: {assunto}")
    return True
