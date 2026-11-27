import torch
import torch.nn as nn

def createNN(input_dim, arch, output_dim):
    layers = []
    prev_dim = input_dim
    for hidden in arch:
        layers.append(nn.Linear(prev_dim, hidden))
        layers.append(nn.ReLU())
        prev_dim = hidden
    layers.append(nn.Linear(prev_dim, output_dim))
    return nn.Sequential(*layers)

lossFn = nn.MSELoss()


# ----------------------
# 逆向 CVAE 模型 (新加入，统一存放)
# ----------------------
class InverseCVAE(nn.Module):
    def __init__(self, input_dim=9, latent_dim=8, output_dim=10):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 512), nn.ReLU(),
            nn.Linear(512, 1024), nn.ReLU()
        )
        self.mean = nn.Linear(1024, latent_dim)
        self.logvar = nn.Linear(1024, latent_dim)

        self.decoder = nn.Sequential(
            nn.Linear(input_dim + latent_dim, 1024), nn.ReLU(),
            nn.Linear(1024, 512), nn.ReLU(),
            nn.Linear(512, output_dim)
        )

    def reparameterize(self, mean, logvar, *, generator):
        std = torch.exp(0.5 * logvar)
        eps = torch.randn(
            std.shape,
            dtype=std.dtype,
            device=std.device,
            generator=generator,
        )
        return mean + eps * std

    def decode(self, z, c):
        # The delivered 2026-11-10 pickle restores its own decoder/latent_dim.
        # Its conditioning order differs from the legacy forward below.
        return self.decoder(torch.cat([z, c], dim=1))

    def forward(self, c, *, generator):
        h = self.encoder(c)
        mean = self.mean(h)
        logvar = self.logvar(h)
        z = self.reparameterize(mean, logvar, generator=generator)
        x_pred = self.decoder(torch.cat([c, z], dim=1))
        return x_pred, mean, logvar
