from .base import PROVIDERS, EmailProvider, EmailSendError, ProviderConfig
from .microsoft_graph import MicrosoftGraphProvider
from .sendgrid import SendGridProvider
from .ses import SesProvider
from .smtp import SmtpProvider

REGISTRY: dict[str, type[EmailProvider]] = {
    "smtp": SmtpProvider,
    "microsoft_graph": MicrosoftGraphProvider,
    "sendgrid": SendGridProvider,
    "ses": SesProvider,
}


def get_provider(config: ProviderConfig) -> EmailProvider:
    try:
        return REGISTRY[config.provider](config)
    except KeyError:
        raise EmailSendError("unsupported_provider", "Unsupported email provider.") from None
