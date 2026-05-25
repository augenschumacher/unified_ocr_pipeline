import streamlit as st
import yaml
import os

CONFIG_FILE = "config.yaml"

def mask_api_key(api_key: str) -> str:
    """Maskiert den API-Schlüssel für die Anzeige (z. B. sk-...1234)."""
    if not api_key:
        return "Nicht gesetzt"
    
    api_key = str(api_key).strip()
    if len(api_key) <= 8:
        return "***"
    
    # Zeige die ersten 3 und die letzten 4 Zeichen
    return f"{api_key[:3]}...{api_key[-4:]}"

def load_config() -> dict:
    """Lädt die Konfiguration aus der config.yaml oder erstellt Standardwerte."""
    default_config = {
        "api_keys": {
            "openai": "",
            "google": "",
            "mistral": ""
        },
        "ollama_host": "http://localhost:11434"
    }
    
    if not os.path.exists(CONFIG_FILE):
        return default_config
        
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
            
        # Merge mit Default-Werten um fehlende Keys zu vermeiden
        if "api_keys" not in config or not isinstance(config["api_keys"], dict):
            config["api_keys"] = {}
            
        for key in default_config["api_keys"]:
            if key not in config["api_keys"]:
                config["api_keys"][key] = ""
                
        if "ollama_host" not in config:
            config["ollama_host"] = default_config["ollama_host"]
            
        return config
    except Exception as e:
        st.error(f"Fehler beim Laden der Konfiguration: {e}")
        return default_config

def save_config(config: dict):
    """Speichert die Konfiguration sicher in der config.yaml."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
        return True
    except Exception as e:
        st.error(f"Fehler beim Speichern der Konfiguration: {e}")
        return False

def render_provider_section(config: dict, provider_id: str, display_name: str):
    """Rendert den Abschnitt für einen spezifischen Provider."""
    with st.expander(f"{display_name} Einstellungen", expanded=False):
        current_key = config["api_keys"].get(provider_id, "")
        
        st.markdown(f"**Aktueller Schlüssel:** `{mask_api_key(current_key)}`")
        
        # Formular für den Provider
        with st.form(key=f"form_{provider_id}"):
            new_key = st.text_input(
                f"Neuen {display_name} API-Schlüssel eingeben",
                type="password",
                placeholder=f"{display_name} Schlüssel hier einfügen..."
            )
            
            submit_button = st.form_submit_button("Schlüssel speichern")
            
            if submit_button:
                if new_key.strip():
                    config["api_keys"][provider_id] = new_key.strip()
                    if save_config(config):
                        st.success(f"{display_name} Schlüssel erfolgreich aktualisiert!")
                        st.rerun()
                else:
                    st.warning("Bitte geben Sie einen gültigen Schlüssel ein.")

def main():
    st.set_page_config(
        page_title="Pipeline Einstellungen",
        page_icon="⚙️",
        layout="centered"
    )
    
    st.title("⚙️ Pipeline Einstellungen")
    st.markdown("Verwalten Sie hier sicher Ihre API-Schlüssel und Verbindungseinstellungen für die OCR-Pipeline.")
    
    # Konfiguration laden
    config = load_config()
    
    # Provider-Bereiche
    render_provider_section(config, "openai", "OpenAI")
    render_provider_section(config, "google", "Google Gemini")
    render_provider_section(config, "mistral", "Mistral")
    
    # Ollama-Bereich
    with st.expander("Ollama Einstellungen", expanded=True):
        current_host = config.get("ollama_host", "http://localhost:11434")
        
        with st.form(key="form_ollama"):
            new_host = st.text_input(
                "Ollama Host URL",
                value=current_host,
                placeholder="http://localhost:11434"
            )
            
            submit_button = st.form_submit_button("Host URL speichern")
            
            if submit_button:
                if new_host.strip():
                    config["ollama_host"] = new_host.strip()
                    if save_config(config):
                        st.success("Ollama Host URL erfolgreich aktualisiert!")
                        st.rerun()
                else:
                    st.warning("Bitte geben Sie eine gültige Host URL ein.")

if __name__ == "__main__":
    main()
