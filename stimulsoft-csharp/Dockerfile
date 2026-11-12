# Stimulsoft Dashboards.WEB NetCore 2024.3.3 — Linux container
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src

# Stimulsoft NuGet: requireLicenseAcceptance
ENV DOTNET_NOLOGO=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1

COPY StimulsoftViewer.csproj ./
RUN dotnet restore StimulsoftViewer.csproj

COPY . ./
# без --no-restore — надёжнее; ошибки компиляции будут в логе
RUN dotnet publish StimulsoftViewer.csproj -c Release -o /app/publish /p:UseAppHost=false

FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS final
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgdiplus \
      libc6-dev \
      fontconfig \
      fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f

COPY --from=build /app/publish .
RUN mkdir -p /data

ENV ASPNETCORE_URLS=http://0.0.0.0:8050 \
    MRT_PATH=/data/Report.mrt
EXPOSE 8050

ENTRYPOINT ["dotnet", "StimulsoftViewer.dll"]
