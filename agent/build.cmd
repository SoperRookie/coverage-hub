@echo off
rem Build lib\covhub-agent.jar -- the Windows twin of agent/build.sh.
rem The two scripts do exactly the same thing; change one, change the other.
rem The reasoning behind each step is documented (in Chinese) in build.sh.
rem
rem KEEP THIS FILE PURE ASCII. cmd.exe does not parse UTF-8 batch files
rem reliably: with non-ASCII text in it, cmd loses track of line boundaries
rem and runs the tail of a comment line as a command. Seen for real with a
rem Chinese-commented version of this very script, under chcp 65001.
rem
rem Usage:    agent\build.cmd      (from cmd or PowerShell, any directory)
rem Requires: JDK 9+ on PATH (javac --release). Target bytecode is Java 8.
setlocal

set "HERE=%~dp0"
set "BUILD=%HERE%build"
set "DEST=%HERE%..\lib\covhub-agent.jar"
set "MAIN=%HERE%src\covhub\agent\CovhubAgent.java"

where javac >nul 2>nul
if errorlevel 1 (
    echo [build] javac not found: install JDK 9+ and put its bin directory on PATH 1>&2
    exit /b 1
)

if exist "%BUILD%" rmdir /s /q "%BUILD%"
mkdir "%BUILD%\classes"
if errorlevel 1 exit /b 1

rem cd into the source root and pass a relative wildcard: javac expands
rem wildcards itself, but not inside a quoted argument -- an absolute
rem "path\*.java" finds nothing once the repo lives in a path with spaces.
rem -Xlint:-options  newer JDKs warn that source 8 is obsolete; 8 is the floor we want.
rem -cp              explicit, so a stale CLASSPATH environment variable (old JDK
rem                  installers leave dt.jar / tools.jar entries behind) cannot
rem                  add a warning that -Werror turns into a build failure.
pushd "%HERE%src"
javac --release 8 -encoding UTF-8 -Xlint:all -Xlint:-options -Werror -cp "%BUILD%\classes" -d "%BUILD%\classes" covhub\agent\*.java
set "JAVAC_RC=%errorlevel%"
popd
if not "%JAVAC_RC%"=="0" (
    echo [build] javac failed 1>&2
    exit /b 1
)

rem The version comes from the VERSION constant in the source: take what is
rem between the double quotes on that line.
set "VERSION="
for /f tokens^=2^ delims^=^" %%v in ('findstr /c:"String VERSION = " "%MAIN%"') do set "VERSION=%%v"
if not defined VERSION (
    echo [build] cannot read CovhubAgent.VERSION 1>&2
    exit /b 1
)

(
    echo Premain-Class: covhub.agent.CovhubAgent
    echo Main-Class: covhub.agent.CovhubAgent
    echo Implementation-Title: covhub agent
    echo Implementation-Version: %VERSION%
) > "%BUILD%\MANIFEST.MF"

jar cfm "%DEST%" "%BUILD%\MANIFEST.MF" -C "%BUILD%\classes" .
if errorlevel 1 (
    echo [build] jar failed 1>&2
    exit /b 1
)

rmdir /s /q "%BUILD%"
for %%f in ("%DEST%") do echo [build] wrote %%~ff ^(version %VERSION%^)
exit /b 0
