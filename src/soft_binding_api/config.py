from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """Application configuration settings"""

    # MongoDB settings
    mongodb_url: str = "mongodb://localhost:27017"
    database_name: str = "c2pa_soft_bindings"

    # API settings
    api_title: str = "C2PA Soft Binding Resolution API"
    api_version: str = "1.1.0"
    api_description: str = "Web service API endpoint for matching soft bindings to C2PA Manifests"

    class Config:
        env_file = ".env"
        case_sensitive = False

settings = Settings()
