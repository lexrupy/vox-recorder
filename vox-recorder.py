#!/usr/bin/env python3
"""
VOX-recorder records audio when there is sound present
Copyright (C) 2015-2025 Gemini AI Assistant (Modifications for Pre-Buffer and Voice Duration)

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.
"""
import json
import os
import shutil
import signal
import sys
import time
import wave
from array import array
from struct import pack
from sys import byteorder

import pyaudio

# Version of the script
__version__ = "2025.12.04.04" # Versão atualizada

# Constants
SILENCE_THRESHOLD = 3000
RECORD_AFTER_SILENCE_SECS = 2
WAVEFILES_STORAGEPATH = "./records"
RATE = 44100
MAXIMUMVOL = 32767
CHUNK_SIZE = 1024
FORMAT = pyaudio.paInt16
PRE_ROLL_SECS = 2 # Pre-roll buffer (segundos)
VOICE_MIN_DURATION_SECS = 0.5 # Min duration before start ro prevent record clicks etc 

class suppress_stdout_stderr(object):
    def __enter__(self):
        self.outnull_file = open(os.devnull, 'w')
        self.errnull_file = open(os.devnull, 'w')

        self.old_stdout_fileno_undup = sys.stdout.fileno()
        self.old_stderr_fileno_undup = sys.stderr.fileno()

        self.old_stdout_fileno = os.dup(sys.stdout.fileno())
        self.old_stderr_fileno = os.dup(sys.stderr.fileno())

        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr

        os.dup2(self.outnull_file.fileno(), self.old_stdout_fileno_undup)
        os.dup2(self.errnull_file.fileno(), self.old_stderr_fileno_undup)

        sys.stdout = self.outnull_file        
        sys.stderr = self.errnull_file
        return self

    def __exit__(self, *_):        
        sys.stdout = self.old_stdout
        sys.stderr = self.old_stderr

        os.dup2(self.old_stdout_fileno, self.old_stdout_fileno_undup)
        os.dup2(self.old_stderr_fileno, self.old_stderr_fileno_undup)

        os.close(self.old_stdout_fileno)
        os.close(self.old_stderr_fileno)

        self.outnull_file.close()
        self.errnull_file.close()

def signal_handler(signum, frame):
    print("\nProgram interrupted by user. Exiting...")
    sys.exit(0)

def show_status(snd_data, record_started, record_started_stamp, wav_filename):
    """Displays volume levels with a VU-meter bar, threshold marker, and indicator for audio presence or recording"""
    
    # 1. OBTÉM A LARGURA ATUAL DO TERMINAL
    try:
        terminal_width = shutil.get_terminal_size().columns
    except:
        # Fallback para terminais sem suporte
        terminal_width = 80 
        
    # Calculate simple VU level for visual feedback
    vu_level = min(int((max(abs(i) for i in snd_data) / MAXIMUMVOL) * 30), 30)
    vu_bar = "█" * vu_level + " " * (30 - vu_level)
    
    # Add a marker for the threshold
    threshold_position = min(int((SILENCE_THRESHOLD / MAXIMUMVOL) * 30), 30)
    if threshold_position < 30:
        vu_bar = vu_bar[:threshold_position] + '|' + vu_bar[threshold_position + 1:]
    
    # Audio presence or recording indicator
    if record_started:
        indicator = '⏺'
        status = "Recording in progress"
    else:
        cycle = int(time.time() * 2) % 2
        indicator = '⏸' if cycle and any(abs(x) > 0 for x in snd_data) else ' '
        status = "Waiting Audio Level"

    # Constrói a primeira parte da mensagem
    main_message = f'VU: [{vu_bar}] | {indicator} {status}'
    
    # Adiciona detalhes do arquivo/tempo, se estiver gravando
    if record_started:
        elapsed = time.time() - record_started_stamp
        detail_message = f' | File: {os.path.basename(wav_filename)}.wav | Time: {elapsed:.1f}s'
    else:
        detail_message = ''

    full_line = main_message + detail_message
    
    # 2. CALCULA ESPAÇO PARA LIMPAR (ADAPTAÇÃO)
    # Garante que a linha completa seja limpa, preenchendo o restante com espaços
    padding_needed = terminal_width - len(full_line)
    
    if padding_needed > 0:
        full_line += ' ' * padding_needed

    # 3. ESCREVE E FORÇA A ATUALIZAÇÃO DA LINHA
    sys.stdout.write('\r' + full_line)
    sys.stdout.flush()

def voice_detected(snd_data):
    """Returns 'True' if sound peaked above the 'silent' threshold"""
    return max(abs(i) for i in snd_data) > SILENCE_THRESHOLD

def normalize(snd_data):
    """Average the volume out"""
    max_amplitude = max(abs(i) for i in snd_data)
    if max_amplitude == 0:
        return snd_data
    times = float(MAXIMUMVOL) / max_amplitude
    return array('h', [int(min(MAXIMUMVOL, max(-MAXIMUMVOL, i * times))) for i in snd_data])

def trim(snd_data):
    """Trim the blank spots at the start and end"""
    def _trim(snd_data):
        record_started = False
        r = array('h')
        for i in snd_data:
            if not record_started and abs(i) > SILENCE_THRESHOLD:
                record_started = True
            if record_started:
                r.append(i)
        return r

    # Trim to the left
    snd_data = _trim(snd_data)
    # Trim to the right
    snd_data.reverse()
    snd_data = _trim(snd_data)
    snd_data.reverse()
    return snd_data

def add_silence(snd_data, seconds):
    """Add silence to the start and end of 'snd_data' of length 'seconds' (float)"""
    silence = array('h', [0 for _ in range(int(seconds * RATE))])
    return silence + snd_data + silence

def wait_for_activity():
    """Listen sound and quit when sound is detected, returning pre-roll buffer."""
    with suppress_stdout_stderr():
        p = pyaudio.PyAudio()
        stream = p.open(format=FORMAT, channels=1, rate=RATE, input=True, frames_per_buffer=CHUNK_SIZE)
    
    # Configuração do buffer de pré-gravação
    frames_per_buffer = RATE * PRE_ROLL_SECS
    buffer_chunks = frames_per_buffer // CHUNK_SIZE
    
    # Cálculo e inicialização para a duração mínima de voz
    min_voice_chunks = max(1, int(VOICE_MIN_DURATION_SECS * RATE / CHUNK_SIZE))
    consecutive_voice_chunks = 0
    
    pre_roll_buffer = [] # Lista para armazenar os chunks
    
    try:
        while True:
            # Lendo um chunk de áudio
            snd_data_raw = stream.read(CHUNK_SIZE, exception_on_overflow=False) 
            snd_data = array('h', snd_data_raw)
            if byteorder == 'big':
                snd_data.byteswap()
            
            # Gerenciamento do Buffer (deve vir antes da checagem de voz)
            pre_roll_buffer.append(snd_data)
            
            # Mantém apenas a quantidade necessária de chunks para o PRE_ROLL_SECS
            if len(pre_roll_buffer) > buffer_chunks:
                pre_roll_buffer.pop(0)

            voice = voice_detected(snd_data)
            show_status(snd_data, False, 0, '')
            
            # Lógica de Duração Mínima de Voz
            if voice:
                consecutive_voice_chunks += 1
                if consecutive_voice_chunks >= min_voice_chunks:
                    # O áudio superou o limiar pelo tempo mínimo necessário
                    break 
            else:
                consecutive_voice_chunks = 0 # Reseta a contagem se encontrar silêncio
                
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()
        
    # Retorna o áudio que estava no buffer, incluindo o chunk de detecção
    return pre_roll_buffer

def record_audio(initial_buffer):
    """Record audio when activity is detected, starting with initial_buffer."""
    # metadata = get_metadata()
    with suppress_stdout_stderr():
        p = pyaudio.PyAudio()
        stream = p.open(format=FORMAT, channels=1, rate=RATE, input=True, frames_per_buffer=CHUNK_SIZE)
        
    # Inicia com o buffer de pré-gravação
    snd_data = array('h')
    for chunk in initial_buffer:
        snd_data.extend(chunk)

    record_started = True
    record_started_stamp = last_voice_stamp = time.time()
    
    # Nome do arquivo simplificado com prefixo 'tx_' e timestamp
    wav_filename = os.path.join(WAVEFILES_STORAGEPATH, f'tx_{time.strftime("%Y%m%d%H%M%S")}')
    
    try:
        while True:
            # Lendo o chunk de áudio
            chunk = array('h', stream.read(CHUNK_SIZE, exception_on_overflow=False))
            if byteorder == 'big':
                chunk.byteswap()
            snd_data.extend(chunk)

            voice = voice_detected(chunk)
            show_status(chunk, record_started, record_started_stamp, wav_filename)

            # A gravação já começou, apenas atualiza o timestamp se houver voz
            if voice:
                last_voice_stamp = time.time()

            # Finaliza a gravação após X segundos de silêncio
            if time.time() > last_voice_stamp + RECORD_AFTER_SILENCE_SECS:
                break
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()

    # Process audio
    snd_data = normalize(snd_data)
    snd_data = trim(snd_data)
    snd_data = add_silence(snd_data, 0.5)

    # Save audio with wave module
    with wave.open(f"{wav_filename}.wav", 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(p.get_sample_size(FORMAT))
        wf.setframerate(RATE)
        wf.writeframes(pack('<' + ('h' * len(snd_data)), *snd_data))

    # Output final message
    endtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))
    record_time = time.time()-record_started_stamp;
    print(f'\n{endtime} recording finished. Record duration {record_time:.1f} seconds. File: {wav_filename}.wav')

    return p.get_sample_size(FORMAT), snd_data, f"{wav_filename}.wav"

def voxrecord():
    """Listen audio from the sound card. If audio is detected, record it to file. After recording,
    start again to wait for next activity"""

    # Register the signal handler for SIGINT (Ctrl-C)
    signal.signal(signal.SIGINT, signal_handler)

    while True:
        # Captura o buffer de pré-gravação
        initial_buffer = wait_for_activity()
        try:
            # Passa o buffer para iniciar a gravação
            _, _, wav_filename = record_audio(initial_buffer)
        except Exception as e:
            print(f"Error during recording: {e}")


if __name__ == '__main__':
    print(f"Voxrecorder v{__version__} started. Hit ctrl-c to quit.")
    
    # Verifica se o diretório existe e se é gravável
    if not os.path.isdir(WAVEFILES_STORAGEPATH):
        # O diretório não existe. Pergunta ao usuário se deve ser criado.
        print(f"the output directory '{WAVEFILES_STORAGEPATH}' does not exist")
        
        # O input() retorna uma string
        create_dir = input("Create it now? (s/n): ").lower()
        
        if create_dir == 's':
            try:
                # Cria o diretório (e todos os pais necessários, se houver)
                os.makedirs(WAVEFILES_STORAGEPATH)
                print(f"Directory '{WAVEFILES_STORAGEPATH}' successfully created.")
                can_proceed = True
            except OSError as e:
                print(f"Error on try to create directory: {e}")
                can_proceed = False
        else:
            print("Aborting....")
            can_proceed = False
            
    elif not os.access(WAVEFILES_STORAGEPATH, os.W_OK):
        # O diretório existe, mas não é gravável.
        print(f"The directory '{WAVEFILES_STORAGEPATH}' exists, but is not writeable. Aborting...")
        can_proceed = False
    else:
        # O diretório existe e é gravável.
        can_proceed = True

    # Inicia o loop principal se puder prosseguir
    if can_proceed:
        try:
            voxrecord()
        except Exception as e:
            print(f"An unexpected error occurred: {e}")    
            
    print("Good bye.")
