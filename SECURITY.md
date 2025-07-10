# Security Policy

## Supported Versions

We actively support the following versions with security updates:

| Version | Supported          |
| ------- | ------------------ |
| 0.13.x  | :white_check_mark: |
| < 0.13  | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it by:

1. **Do NOT** open a public GitHub issue
2. Email the maintainers at [security contact - to be filled by maintainers]
3. Include detailed information about the vulnerability
4. Allow reasonable time for the issue to be addressed before public disclosure

## Security Measures

This project implements the following security measures:

### Automated Security Scanning
- **Dependabot**: Automatically scans for vulnerable dependencies weekly
- **pip-audit**: Runs security audits on all Python dependencies
- **CodeQL**: Performs static analysis for security vulnerabilities
- **GitHub Security Advisories**: Monitors for known vulnerabilities

### CI/CD Security
- Security scans run on every pull request
- Builds fail if high-severity vulnerabilities are detected
- Regular scheduled security scans

### Dependency Management
- Dependencies are regularly updated
- Security patches are prioritized
- Minimal dependency footprint maintained

## Security Best Practices

When contributing to this project:

1. Keep dependencies up to date
2. Use secure coding practices
3. Validate all inputs
4. Follow the principle of least privilege
5. Report security concerns promptly

## Security Updates

Security updates will be:
- Released as soon as possible after discovery
- Documented in release notes
- Communicated through GitHub Security Advisories
- Backported to supported versions when necessary