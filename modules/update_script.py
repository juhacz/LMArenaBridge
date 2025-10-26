# update_script.py
import os
import shutil
import time
import subprocess
import sys
import json
import re

def _parse_jsonc(jsonc_string: str) -> dict:
    """
    Solidne parsowanie ciągu JSONC — usuwa komentarze.
    """
    lines = jsonc_string.splitlines()
    no_comments_lines = []
    in_block_comment = False
    for line in lines:
        stripped_line = line.strip()
        if in_block_comment:
            if '*/' in stripped_line:
                in_block_comment = False
                line = stripped_line.split('*/', 1)[1]
            else:
                continue
        
        if '/*' in line and not in_block_comment:
            before_comment, _, after_comment = line.partition('/*')
            if '*/' in after_comment:
                _, _, after_block = after_comment.partition('*/')
                line = before_comment + after_block
            else:
                line = before_comment
                in_block_comment = True

        if line.strip().startswith('//'):
            continue
        
        no_comments_lines.append(line)

    return json.loads("\n".join(no_comments_lines))

def load_jsonc_values(path):
    """Wczytuje wartości z pliku .jsonc, ignorując komentarze — zwraca słownik klucz-wartość."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        return _parse_jsonc(content)
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        print(f"Błąd podczas ładowania lub parsowania {path}: {e}")
        return None

def get_all_relative_paths(directory):
    """Zwraca zbiór względnych ścieżek wszystkich plików i pustych katalogów w danym katalogu."""
    paths = set()
    for root, dirs, files in os.walk(directory):
        # Dodaj pliki
        for name in files:
            path = os.path.join(root, name)
            paths.add(os.path.relpath(path, directory))
        # Dodaj puste katalogi
        for name in dirs:
            dir_path = os.path.join(root, name)
            if not os.listdir(dir_path):
                paths.add(os.path.relpath(dir_path, directory) + os.sep)
    return paths

def main():
    print("--- Skrypt aktualizacyjny uruchomiony ---")
    
    # 1. Czekamy na zamknięcie głównego programu
    print("Oczekiwanie na zamknięcie głównego programu (3 sekundy)...")
    time.sleep(3)
    
    # 2. Definicja ścieżek
    destination_dir = os.getcwd()
    update_dir = "update_temp"
    source_dir_inner = os.path.join(update_dir, "LMArenaBridge-main")
    config_filename = 'config.jsonc'
    models_filename = 'models.json'
    model_endpoint_map_filename = 'model_endpoint_map.json'
    
    if not os.path.exists(source_dir_inner):
        print(f"Błąd: nie znaleziono katalogu źródłowego {source_dir_inner}. Aktualizacja przerwana.")
        return
        
    print(f"Katalog źródłowy: {os.path.abspath(source_dir_inner)}")
    print(f"Katalog docelowy: {os.path.abspath(destination_dir)}")

    # 3. Tworzymy kopię zapasową kluczowych plików
    print("Tworzę kopię zapasową obecnych plików konfiguracyjnych i modeli...")
    old_config_path = os.path.join(destination_dir, config_filename)
    old_models_path = os.path.join(destination_dir, models_filename)
    old_config_values = load_jsonc_values(old_config_path)
    
    # 4. Określamy elementy do zachowania
    # Zachowujemy update_temp, katalog .git i .github oraz potencjalne ukryte pliki użytkownika
    preserved_items = {update_dir, ".git", ".github"}

    # 5. Pobieramy listy plików nowych i obecnych
    new_files = get_all_relative_paths(source_dir_inner)
    # Wykluczamy .git i .github — nie wdrażamy tych katalogów
    new_files = {f for f in new_files if not (f.startswith('.git') or f.startswith('.github'))}

    current_files = get_all_relative_paths(destination_dir)

    print("\n--- Analiza zmian w plikach ---")
    print("[*] Funkcja usuwania plików jest wyłączona, aby chronić dane użytkownika. Wykonywane będą tylko kopiowanie plików i aktualizacja konfiguracji.")

    # 7. Kopiowanie nowych plików (z wyłączeniem plików konfiguracyjnych)
    print("\n[+] Kopiowanie nowych plików...")
    try:
        new_config_template_path = os.path.join(source_dir_inner, config_filename)
        
        for item in os.listdir(source_dir_inner):
            s = os.path.join(source_dir_inner, item)
            d = os.path.join(destination_dir, item)
            
            # Pomijamy katalogi .git i .github
            if item in {".git", ".github"}:
                continue
            
            if os.path.basename(s) == config_filename:
                continue # Pomijamy główny plik konfiguracyjny, będzie obsłużony osobno
            
            if os.path.basename(s) == model_endpoint_map_filename:
                continue # Pomijamy model_endpoint_map.json — zachowujemy lokalną wersję użytkownika

            if os.path.basename(s) == models_filename:
                continue # Pomijamy models.json — zachowujemy lokalną wersję użytkownika

            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        print("Kopiowanie plików zakończone pomyślnie.")

    except Exception as e:
        print(f"Wystąpił błąd podczas kopiowania plików: {e}")
        return

    # 8. Inteligentne scalanie konfiguracji
    if old_config_values and os.path.exists(new_config_template_path):
        print("\n[*] Wykonuję inteligentne scalanie konfiguracji (zachowując komentarze)...")
        try:
            with open(new_config_template_path, 'r', encoding='utf-8') as f:
                new_config_content = f.read()

            new_version_values = load_jsonc_values(new_config_template_path)
            new_version = new_version_values.get("version", "unknown")
            old_config_values["version"] = new_version

            for key, value in old_config_values.items():
                if isinstance(value, str):
                    replacement_value = f'"{value}"'
                elif isinstance(value, bool):
                    replacement_value = str(value).lower()
                else:
                    replacement_value = str(value)
                
                pattern = re.compile(f'("{key}"\s*:\s*)(?:".*?"|true|false|[\d\.]+)')
                if pattern.search(new_config_content):
                    new_config_content = pattern.sub(f'\\g<1>{replacement_value}', new_config_content)

            with open(old_config_path, 'w', encoding='utf-8') as f:
                f.write(new_config_content)
            print("Scalanie konfiguracji zakończone pomyślnie.")

        except Exception as e:
            print(f"Podczas scalania konfiguracji wystąpił poważny błąd: {e}")
    else:
        print("Nie można wykonać inteligentnego scalania — zostanie użyty nowy plik konfiguracyjny.")
        if os.path.exists(new_config_template_path):
            shutil.copy2(new_config_template_path, old_config_path)

    # 9. Czyszczenie katalogu tymczasowego
    print("\n[*] Czyszczenie plików tymczasowych...")
    try:
        shutil.rmtree(update_dir)
        print("Czyszczenie zakończone.")
    except Exception as e:
        print(f"Wystąpił błąd podczas czyszczenia plików tymczasowych: {e}")

    # 10. Ponowne uruchomienie głównego programu
    print("\n[*] Ponowne uruchamianie głównego programu...")
    try:
        main_script_path = os.path.join(destination_dir, "api_server.py")
        if not os.path.exists(main_script_path):
            print(f"Błąd: nie znaleziono głównego skryptu {main_script_path}.")
            return
        
        subprocess.Popen([sys.executable, main_script_path])
        print("Główny program został uruchomiony w tle.")
    except Exception as e:
        print(f"Nie udało się ponownie uruchomić głównego programu: {e}")
        print(f"Uruchom ręcznie: {main_script_path}")

    print("--- Aktualizacja zakończona ---")

if __name__ == "__main__":
    main()