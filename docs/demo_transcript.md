# Demo transcript

Real terminal output (ANSI colours stripped) from `python -m client.cli` against the running backend, provider **Gemini**, model **gemini-3.8-flash**, logged in as **E1001 (Aarav Mehta)**, recorded 2026-10-10. The status line under each reply is printed by the CLI: request ID, status, intent, tools, input/output tokens, cost, time to first visible text, and total time.

A screen recording / GIF of the token-by-token streaming should be recorded from a live terminal (see README › Recording the demo); a text transcript can't show tokens arriving.

## 1. General HR question answered (prompt v1.1)

```text
$ python -m client.cli --session demo1b -q "What's the difference between gross and net salary?"
HR Buddy: **Gross salary** is the total amount you earn before any deductions are made. It includes your basic pay, allowances (such as HRA or travel allowance), and any bonuses or overtime pay.

**Net salary** (also known as "take-home pay") is the actual amount credited to your bank account after mandatory and voluntary deductions—such as income tax (TDS), Provident Fund (PF), and professional tax—are subtracted from your gross salary. 

You can see a detailed breakdown of your gross components and deductions on your monthly payslip in the HRMS portal.
[5e9e212098db] status=success intent=general_hr tools=[] tokens=1861/192 cost=$0.00211575 first_text=4212.0ms total=4880.3ms
```

<details><summary>Same question on prompt v1.0 — over-refusal found during the demo (diagnosis finding 2)</summary>

```text
$ python -m client.cli --session demo1 -q What's the difference between gross and net salary?
HR Buddy: The policy documents provided to me don't cover payroll definitions like gross versus net salary, and I don't have company-specific details on this topic. 

Would you like me to raise a ticket with the HR team so they can assist you with your salary structure questions?
[96fc2a9e8976] status=success intent=general_hr tools=[] tokens=1736/100 cost=$0.001677 first_text=4631.5ms total=4932.6ms
```

</details>

## 2. Ticket classified into structured JSON

```text
$ python -m client.cli --classify My September salary hasn't been credited and my rent is due on the 5th
{
  "request_id": "79cf02a93efe",
  "ticket": {
    "category": "payroll",
    "urgency": "high",
    "requires_human_review": true,
    "needs_clarification": false,
    "clarification_question": null,
    "summary": "September salary has not been credited.",
    "urgency_reason": "Salary not received and employee has an upcoming rent deadline on the 5th."
  },
  "validation": {
    "valid": true,
    "attempts": [
      {
        "attempt": 1,
        "status": "success",
        "finish_reason": "stop",
        "issues": [],
        "repaired": false,
        "raw": "{\"category\":\"payroll\",\"urgency\":\"high\",\"requires_human_review\":true,\"needs_clarification\":false,\"clarification_question\":null,\"summary\":\"September salary has not been credited.\",\"urgency_reason\":\"Salary not received and employee has an upcoming rent deadline on the 5th.\"}"
      }
    ],
    "fallback_used": false
  },
  "usage": {
    "input_tokens": 870,
    "output_tokens": 64,
    "cost_usd": 0.0008925,
    "latency_ms": 2733.4
  }
}

$ python -m client.cli --session demo2 -q THIS IS UNACCEPTABLE!!! My payslip PDF has my middle name misspelled!!! FIX IT NOW!!!
HR Buddy: 
{
  "ticket_id": "HR-92549D",
  "category": "payroll",
  "urgency": "low",
  "requires_human_review": true,
  "needs_clarification": false,
  "clarification_question": null,
  "summary": "Employee reports that their middle name is misspelled on their payslip PDF.",
  "urgency_reason": "Minor typo on a document with no pay disruption, work blockage, or stated deadline (angry tone ignored).",
  "schema_valid": true
}
I've logged ticket HR-92549D for the HR team — category: payroll, urgency: low. An HR agent will review it personally. Summary: Employee reports that their middle name is misspelled on their payslip PDF.
[87d7ae133cf2] status=success intent=ticket tools=[] tokens=1828/120 cost=$0.001821 first_text=4841.4ms total=4842.7ms
```

## 3. Policy question answered from supplied text

```text
$ python -m client.cli --session demo3 -q Can I work from home on Wednesdays if my manager agrees?
HR Buddy: No, you cannot work from home on Wednesdays, even if your manager agrees. Wednesday is the company-wide Anchor Day, and WFH is not permitted for any employee regardless of manager approval, unless you have an approved medical accommodation logged with HR (WFH Policy §3).
[83e203d9cdd6] status=success intent=policy_question tools=[] tokens=2093/120 cost=$0.00201975 first_text=8758.0ms total=9011.1ms
```

## 4. Leave-balance lookup via the deterministic tool

```text
$ python -m client.cli --session demo4 -q How many casual leaves do I have left?
HR Buddy: You currently have **3** casual leave days remaining for the 2026–27 leave year (out of an entitlement of 8 days, with 5 used). 

Please remember that casual leave cannot be carried forward and lapses on 31 March (Leave Policy §2).
[7e57d8062599] status=success intent=personal_record tools=['get_leave_balance'] tokens=3769/296 cost=$0.00393675 first_text=12444.3ms total=12446.1ms
```

## 4b. Missing info -> assistant asks, then tool call

```text
$ python -m client.cli --session demo4b -q What's the status of my reimbursement?
HR Buddy: Sure, I can check that for you! Could you please share the claim ID? It should be in the format `RMB-####` and can be found in the HRMS portal (Reimbursement Policy §7).
[d80ba2142c65] status=success intent=personal_record tools=[] tokens=2183/111 cost=$0.0020535 first_text=5362.7ms total=5364.3ms

$ python -m client.cli --session demo4b -q It's RMB-2077
HR Buddy: Your reimbursement claim **RMB-2077** for travel meals (INR 3,250), submitted on 2026-09-28, is currently **pending manager approval**. 

Once approved, it will be processed and paid with your next month's salary (Reimbursement Policy §6).
[8d0f545dc156] status=success intent=personal_record tools=['get_reimbursement_status'] tokens=3739/176 cost=$0.00346425 first_text=8020.8ms total=8022.2ms
```

## 4c. Streamed answer after a tool call

```text
$ python -m client.cli --session demo-stream-tool -q "How many sick and earned leaves do I have?"
HR Buddy: You currently have:

- **Sick Leave:** 9 days remaining (out of 10 entitled, 1 used)
- **Earned Leave:** 12 days remaining from your current entitlement (out of 18, 6 used), plus 4 days carried forward.
[aa6a676d8a9e] status=success intent=personal_record tools=['get_leave_balance'] tokens=4106/229 cost=$0.00393825 first_text=9265.3ms total=9528.0ms
```

_(Recorded before the earned-leave records were made arithmetically consistent; the model added the 4 carried-forward days on top of `remaining`. After the data fix it answers "16 days (18 entitled + 4 carried forward − 6 used)".)_

## 5. Out-of-scope declined

```text
$ python -m client.cli --session demo5 -q Write me a poem about cricket
HR Buddy: I'm the Acme HR helpdesk assistant, so I can only help with HR topics — leave, work-from-home, reimbursements, payroll questions, your own leave balance or claim status, or raising an HR ticket.
[ea7854cb1d7c] status=declined_out_of_scope intent=out_of_scope tools=[] tokens=942/42 cost=$0.000864 first_text=2941.7ms total=2943.5ms

$ python -m client.cli --session demo5 -q asdkjh qwe zzz
HR Buddy: I'm the Acme HR helpdesk assistant, so I can only help with HR topics — leave, work-from-home, reimbursements, payroll questions, your own leave balance or claim status, or raising an HR ticket.
[83bc219f235d] status=declined_out_of_scope intent=out_of_scope tools=[] tokens=0/0 cost=$0 first_text=2.8ms total=3.6ms
```

## 6. Streaming vs non-streaming

```text
$ python -m client.cli --session demo6 --compare Explain the work-from-home policy, including the location rules, in detail.
Query: Explain the work-from-home policy, including the location rules, in detail.

--- NON-STREAMED ---
HR Buddy: Here are the key details of the Work-From-Home (WFH) policy:

- **Eligibility & Allowance:** Confirmed employees can take up to 2 WFH days per week; those in their first 90 days of probation must work from the office every day. Unused days do not roll over (WFH Policy §1, §2).
- **Anchor Day:** WFH is not allowed on Wednesdays for any employee, unless there is an approved medical accommodation logged with HR (WFH Policy §3).
- **Location Rules:** WFH must be done from your registered home city. Working from any other city is classified as "Work from Anywhere" (WFA), which is capped at 10 working days per calendar year, requires manager and HR approval 7 days in advance, and is strictly prohibited outside India (WFH Policy §4, §5).
- **Core Hours & Equipment:** You must be reachable between 11:00 and 16:00 IST and work only on your company laptop (WFH Policy §6, §7).
- **Requesting:** Mark your WFH days in the HRMS portal; manager approval is not needed within your 2-day weekly allowance (WFH Policy §8).
[0ce0fa65ea04] status=success intent=policy_question tools=[] tokens=2103/309 cost=$0.002736 first_text=8022.5ms total=8022.6ms

--- STREAMED ---
HR Buddy: Here are the key details of the Work-From-Home (WFH) policy:

- **Eligibility & Allowance:** Confirmed employees may take up to 2 WFH days per week. Employees in their first 90 days (probation) are not eligible and must work from the office. Unused days do not roll over (WFH Policy §1, §2).
- **Anchor Day:** Wednesday is the mandatory company-wide Anchor Day; WFH is not permitted on Wednesdays without an approved medical accommodation logged with HR (WFH Policy §3).
- **Location Rules:** WFH must be done from your registered home city (WFH Policy §4). Working from another city counts as "Work from Anywhere" (WFA), which is limited to 10 working days per calendar year, requires manager and HR approval at least 7 days in advance, and is strictly prohibited outside India (WFH Policy §5).
- **Hours & Requests:** Employees must be reachable during core hours (11:00–16:00 IST), use company laptops only, and mark WFH days directly in the HRMS portal (WFH Policy §6, §7, §8).
[77e12e63457d] status=success intent=policy_question tools=[] tokens=2420/320 cost=$0.003015 first_text=8308.7ms total=10027.5ms

mode            time to first text    total time
non-streamed              8022.5ms      8022.6ms
streamed                  8308.7ms     10027.5ms
```

## 7. Memory survives a server restart

Turns for session `demo4b` (sections 4b) were stored at **10:28** in `data/runtime/memory.db`. The backend was then stopped and
restarted (current process started **10:37:03**). A new question in the same session:

```text
$ python -m client.cli --session demo4b -q "Remind me which claim we discussed earlier and its status?"
HR Buddy: We discussed claim **RMB-2077** for travel meals (INR 3,250), submitted on 2026-09-28. 

Its current status is **pending manager approval**. Once approved, the amount will be paid out with your next month's salary (Reimbursement Policy §6).
[ddbd346d0b6c] status=success intent=personal_record tools=['get_reimbursement_status'] tokens=4570/418 cost=$0.004995 first_text=10014.2ms total=10422.2ms
```

Rows in SQLite at that point (`select role, content, created_at from turns where session_id='demo4b'`):

```text
user       What's the status of my reimbursement?                 2026-10-10 10:28:11
assistant  Sure, I can check that for you! Could you please ...   2026-10-10 10:28:11
user       It's RMB-2077                                           2026-10-10 10:28:19
assistant  Your reimbursement claim **RMB-2077** for travel ...    2026-10-10 10:28:19
user       Which claim were we just talking about?                2026-10-10 10:28:45
assistant  We were just talking about claim **RMB-2077** ...       2026-10-10 10:28:45
```

Only user/assistant text is stored — no system prompt, policy text or raw tool payloads. The claim ID in the stored user
turn also satisfies the tool's argument-provenance check, so the assistant could re-query the live status.

## 8. Broken client token

```text
$ python -m client.cli --token wrong -q "How many casual leaves do I have left?"

HTTP 401: {"error": "unauthorized", "request_id": "53b3224da9b0"}
```

## Streaming vs non-streaming — what we observed

| Mode | Time to first visible text | Total |
|---|---|---|
| Non-streamed | 8,023 ms | 8,023 ms |
| Streamed | 8,309 ms | 10,028 ms |

The two requests ran back to back, so their timings can't be compared exactly. For a long answer, streaming put text on screen around 1.7 s before the
answer finished generating (the provider sent 8 chunks between 4.7 s and 6.4 s of the answer call). The perceived gain is smaller than
expected for two measured reasons:
1. **Pre-stream work dominates.** The router model call (~2.8–3.6 s) runs before the answer stream starts, and nothing can
   stream until we know which path (tools / policy / decline) to take.
2. **Gemini's first chunk is slow and chunks are coarse.** A raw stream test showed the first chunk at ~4.7 s, then ~120-character
   chunks rather than single tokens.

In the terminal the difference is still clear: in non-streamed mode the screen is blank until the whole answer appears at once.
In streamed mode the answer types itself out, and the user sees progress for the last part of the wait. The bigger win would be
cutting the router latency, e.g. a smaller/faster router model, or skipping the router call when the deterministic pre-filters already decide.
