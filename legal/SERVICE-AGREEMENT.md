# SHACKLE IMPLEMENTATION & ARCHITECTURE AUDIT
## SERVICE AGREEMENT

**Agreement Date:** _________________________________  
**Service Fee:** $2,500.00 USD (paid via Stripe)

**Provider:**  
Sovereign Logic  
Represented by: Dante Bullock  
Email: docspoc101@gmail.com  
GitHub: https://github.com/Fame510/SHACKLE

**Client:**  
Name: _________________________________  
Company: _________________________________  
Email: _________________________________  
GitHub/Contact: _________________________________

---

## 1. SERVICES PROVIDED

Sovereign Logic agrees to provide the following services ("Services") to Client:

### 1.1 Architecture Audit
- Comprehensive review of Client's agent codebase
- Identification of potential infinite loops, token burn patterns, and cost vulnerabilities
- Analysis of LLM call patterns, tool usage, and failure modes
- Written audit report (PDF or Markdown format)

### 1.2 Custom SHACKLE Configuration
- Framework-specific integration plan (CrewAI, LangGraph, AutoGen, or other supported frameworks)
- Custom configuration file with optimized parameters:
  - `budget` limits based on Client's typical usage patterns
  - `max_repeat_calls` thresholds for loop detection
  - `timeout_seconds` values appropriate to Client's workflows
  - Tool-specific overrides where applicable
- Justification document explaining configuration choices

### 1.3 Integration Guide
- Step-by-step installation instructions
- Exact code modifications required (file names, line numbers, code blocks)
- Framework-specific examples
- Testing and verification checklist

### 1.4 30-Day Support Period
- Email support for integration questions and troubleshooting
- Response time: within 48 hours (business days)
- Scope: bug fixes, configuration adjustments, integration assistance
- **Excludes:** new feature development, unrelated code issues, general consulting beyond SHACKLE integration

---

## 2. CLIENT OBLIGATIONS

Client agrees to:

### 2.1 Provide Code Access
Within **5 business days** of payment, Client shall provide ONE of the following:
- GitHub repository URL with read access granted to Sovereign Logic
- Zip file containing complete codebase via email
- Access credentials to private repository (GitLab, Bitbucket, etc.)

### 2.2 Provide Context
Client shall provide:
- Brief description of agent's purpose and workflow
- Known cost or loop issues (if any)
- Target frameworks (CrewAI, LangGraph, AutoGen, etc.)
- API keys or test environment access (optional but recommended for testing)

### 2.3 Code Requirements
Client's codebase must:
- Be written in Python 3.10 or higher
- Use one or more supported frameworks (CrewAI, LangGraph, AutoGen)
- Be reasonably documented (comments, variable names)

**Note:** If codebase is incomplete, undocumented, or uses unsupported frameworks, Sovereign Logic will notify Client within 3 business days and offer a refund or renegotiation.

---

## 3. DELIVERY TIMELINE

### 3.1 Standard Delivery
- **Audit Report & Configuration:** Within **48-72 hours** of receiving code access
- **Delivery Method:** Email to Client's registered email address

### 3.2 Delays
If Client-provided code requires clarification or additional information:
- Sovereign Logic will notify Client within 24 hours
- Delivery timeline pauses until Client responds
- If no response within 7 days, services are considered complete "as-is"

---

## 4. DELIVERABLES

Client will receive:

1. **Architecture Audit Report** (PDF or Markdown)
   - Identified vulnerabilities and loop risks
   - Cost analysis and token usage patterns
   - Recommendations summary

2. **Custom SHACKLE Configuration File** (`shackle_config.py` or similar)
   - Ready-to-use Python code
   - Inline comments explaining each parameter

3. **Integration Guide** (Markdown or PDF)
   - Step-by-step installation instructions
   - Exact code changes with file/line references
   - Testing checklist

4. **30-Day Email Support**
   - Starts from delivery date
   - Email: docspoc101@gmail.com
   - Response within 48 business hours

---

## 5. PAYMENT TERMS

### 5.1 Payment
- **Amount:** $2,500.00 USD
- **Method:** Stripe payment link: https://buy.stripe.com/6oU28q54DbsXdpV6Hy9sk00
- **Timing:** Payment in full before services commence

### 5.2 Refund Policy
- **Full Refund:** Available within 5 business days of payment if:
  - Client cancels before providing code access
  - Sovereign Logic determines codebase is incompatible with SHACKLE
  - Services cannot be delivered as described

- **No Refund:** After deliverables are provided, unless:
  - Deliverables are materially deficient (missing promised components)
  - Client requests refund within 7 days of delivery with documented deficiencies

### 5.3 No Recurring Charges
This is a **one-time service fee**. No subscription or recurring billing.

---

## 6. INTELLECTUAL PROPERTY

### 6.1 SHACKLE License
- SHACKLE software remains open-source under AGPLv3
- Client receives the same rights as any user of the open-source project
- Custom configuration and audit documents are Client's property

### 6.2 Client Code
- Client retains all rights to their codebase
- Sovereign Logic will not use, share, or retain Client code beyond the service period (except as required for support obligations)

### 6.3 Audit Reports
- Audit reports and configuration files are Client's confidential property
- Sovereign Logic may create anonymized case studies (with Client's written consent only)

---

## 7. CONFIDENTIALITY

Both parties agree to maintain confidentiality of:
- Client's source code and business information
- Configuration details and audit findings
- Any API keys, credentials, or access tokens shared

**See attached NDA for full confidentiality terms.**

---

## 8. WARRANTIES AND DISCLAIMERS

### 8.1 Sovereign Logic Warrants
- Services will be performed in a professional and workmanlike manner
- Deliverables will match the description in Section 1
- Custom configuration will be based on industry best practices

### 8.2 DISCLAIMER
SHACKLE SOFTWARE IS PROVIDED "AS IS" UNDER AGPLv3 LICENSE.

**Sovereign Logic does NOT warrant:**
- That SHACKLE will prevent 100% of cost overruns or loops
- That integration will have zero impact on Client's existing code
- That Client's agents will function identically after SHACKLE integration
- Compatibility with future framework updates or API changes

**LLM orchestration is inherently non-deterministic. SHACKLE is a best-effort circuit breaker, not a guarantee.**

---

## 9. LIMITATION OF LIABILITY

### 9.1 Maximum Liability
Sovereign Logic's total liability under this Agreement shall not exceed the **service fee paid ($2,500.00)**.

### 9.2 Excluded Damages
**IN NO EVENT SHALL SOVEREIGN LOGIC BE LIABLE FOR:**
- Indirect, incidental, special, or consequential damages
- Lost profits, revenue, or data
- Cost of substitute services
- Damages resulting from Client's use or misuse of SHACKLE
- API costs incurred before, during, or after SHACKLE integration

### 9.3 Client Responsibility
Client remains solely responsible for:
- Monitoring their own API usage and costs
- Testing SHACKLE integration in their environment before production use
- Setting appropriate budget limits and circuit breaker thresholds
- Maintaining backups of their code before integration

---

## 10. SUPPORT TERMS

### 10.1 Support Scope (30 Days)
**Included:**
- Bug fixes in delivered configuration
- Clarification of integration instructions
- Assistance with SHACKLE-specific errors
- Minor configuration adjustments

**NOT Included:**
- Debugging Client's underlying agent code
- Adding new features to SHACKLE
- Re-architecture of Client's agent system
- Support beyond 30 days from delivery

### 10.2 Extended Support
Post-30-day support available at:
- **$200/hour** for ad-hoc consulting
- **$500/month** retainer (5 hours included)

---

## 11. TERMINATION

### 11.1 By Client
Client may terminate before code access is provided for a full refund (minus Stripe processing fees).

### 11.2 By Sovereign Logic
If Client fails to provide code access within 30 days of payment, Sovereign Logic may terminate and retain 50% of payment for administrative costs.

### 11.3 Effect of Termination
- Client retains any deliverables already provided
- Support obligations end immediately
- Confidentiality obligations survive termination

---

## 12. GENERAL PROVISIONS

### 12.1 Entire Agreement
This Agreement, together with the attached NDA, constitutes the entire agreement between the parties.

### 12.2 Governing Law
This Agreement shall be governed by the laws of the **State of California**, USA, without regard to conflict of law principles.

### 12.3 Dispute Resolution
Any disputes shall be resolved through:
1. Good faith negotiation (30 days)
2. Mediation (if negotiation fails)
3. Binding arbitration in Oakland, California (if mediation fails)

### 12.4 Amendment
This Agreement may only be amended by written agreement signed by both parties.

### 12.5 Assignment
Client may not assign this Agreement without Sovereign Logic's written consent.

### 12.6 Severability
If any provision is unenforceable, the remaining provisions remain in full force.

### 12.7 Force Majeure
Neither party is liable for delays due to circumstances beyond reasonable control (natural disasters, internet outages, etc.).

---

## 13. ACCEPTANCE

**By making payment via the Stripe link, Client acknowledges:**
- They have read and understood this Agreement
- They agree to be bound by all terms
- They have authority to enter into this Agreement on behalf of their company (if applicable)

**Electronic acceptance (via Stripe payment) constitutes a legally binding signature.**

---

## CONTACT INFORMATION

**For service delivery and support:**  
Email: docspoc101@gmail.com

**For billing/payment questions:**  
Stripe payment link: https://buy.stripe.com/6oU28q54DbsXdpV6Hy9sk00

**For legal questions:**  
Email: docspoc101@gmail.com (Subject: "Legal - SHACKLE Agreement")

---

## SIGNATURES

**SOVEREIGN LOGIC**

Signature: _________________________________  
Name: Dante Bullock  
Title: Founder  
Date: _________________________________

**CLIENT**

Signature: _________________________________  
Name: _________________________________  
Title: _________________________________  
Company: _________________________________  
Date: _________________________________

---

**Payment Confirmation:**  
Stripe Transaction ID: _________________________________  
Payment Date: _________________________________  
Amount Paid: $2,500.00 USD

---

*This Agreement is effective upon receipt of payment. Client will receive a copy via email within 24 hours of payment.*
