#!/usr/bin/env python3
"""Gera tela/icone.ico (lupa branca num quadrado azul), usado no atalho da área de trabalho e como
ícone da página. Só biblioteca padrão; rode de novo se quiser mudar as cores:

    python scripts/gerar_icone.py
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

AZUL = (0x1F, 0x5F, 0xAE)
BRANCO = (0xFF, 0xFF, 0xFF)
TAMANHOS = (16, 24, 32, 48, 64)


def _pixel(x: float, y: float, n: int) -> tuple[int, int, int, int]:
    """Cor (R, G, B, A) no ponto (x, y) de um ícone n×n, em coordenadas 0..1."""
    raio_canto = 0.18
    # Quadrado de cantos arredondados
    dx = max(raio_canto - x, 0, x - (1 - raio_canto))
    dy = max(raio_canto - y, 0, y - (1 - raio_canto))
    if math.hypot(dx, dy) > raio_canto:
        return (0, 0, 0, 0)
    # Lupa: anel + cabo na diagonal
    cx, cy, r, espessura = 0.43, 0.43, 0.22, 0.075 if n > 16 else 0.1
    no_anel = abs(math.hypot(x - cx, y - cy) - r) <= espessura / 2
    # Cabo: segmento de (cx + r·cos45, cy + r·sin45) até (0.8, 0.8)
    ax, ay = cx + r * 0.7071, cy + r * 0.7071
    bx, by = 0.8, 0.8
    t = max(0.0, min(1.0, ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / ((bx - ax) ** 2 + (by - ay) ** 2)))
    no_cabo = math.hypot(x - (ax + t * (bx - ax)), y - (ay + t * (by - ay))) <= espessura * 0.75
    r, g, b = BRANCO if no_anel or no_cabo else AZUL
    return (r, g, b, 255)


def _imagem(n: int) -> bytes:
    """BMP 32 bits (BGRA, de baixo para cima) com máscara AND, como o formato .ico pede."""
    amostras = 4  # antisserrilhado: 4×4 amostras por pixel
    linhas: list[bytes] = []
    for py in range(n - 1, -1, -1):
        linha = bytearray()
        for px in range(n):
            soma = [0, 0, 0, 0]
            for sy in range(amostras):
                for sx in range(amostras):
                    cor = _pixel((px + (sx + 0.5) / amostras) / n, (py + (sy + 0.5) / amostras) / n, n)
                    for i in range(4):
                        soma[i] += cor[i] * (cor[3] if i < 3 else 255)
            alfa = soma[3] // (amostras**2 * 255)
            rgb = [soma[i] // max(soma[3] // 255, 1) for i in range(3)] if alfa else [0, 0, 0]
            linha += bytes((rgb[2], rgb[1], rgb[0], alfa))
        linhas.append(bytes(linha))
    cabecalho = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    mascara = b"\x00" * (((n + 31) // 32) * 4 * n)
    return cabecalho + b"".join(linhas) + mascara


def gerar(caminho: Path) -> None:
    imagens = [_imagem(n) for n in TAMANHOS]
    deslocamento = 6 + 16 * len(imagens)
    diretorio = b""
    for n, dados in zip(TAMANHOS, imagens, strict=True):
        lado = 0 if n >= 256 else n
        diretorio += struct.pack("<BBBBHHII", lado, lado, 0, 0, 1, 32, len(dados), deslocamento)
        deslocamento += len(dados)
    caminho.write_bytes(struct.pack("<HHH", 0, 1, len(imagens)) + diretorio + b"".join(imagens))


if __name__ == "__main__":
    destino = Path(__file__).resolve().parent.parent / "tela" / "icone.ico"
    gerar(destino)
    print(f"Gerado {destino} ({destino.stat().st_size:,} bytes)")
