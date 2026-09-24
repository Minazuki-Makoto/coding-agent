## coding harness

## Above all , I will introduce the modules I prepare to use
·1.Agent loop 

    As for the core of the agent, it gives agent a basic way to think,act,observation. And the agent need to follow the program

·2.MCP tools

    The tools agent may use in the workflow need to be packaged by mcp_server, it can give tools a standard access port.LLM can load all the tools' info 
    to choose what to use

·3.Context

    it contains:
                (1) get infos from github,Internet .....
                (2) get the history chat in the same session
    
    then clear some unuseful contents to decrease the context window,with the purpose of easing the illusory.
    After that , combined with the nearest unachieved goals, giving it to the llm

·4.Skills

    it has the combination of different kind of tools,to give model a ability to deal with one pointed assignment
    Different skills can enhance the model to deal with different assignments with less thinking-token & thinking time
    it consists of :
                (1) description of the skill
                (2) scripts(run the skill)

·5.memory

    it makes LLM to storage the history chat,containing:
                (1) state.jsonl : the thinking process and results and primary state (easy to date back to the primary state if some problems in the process) and solved plans and unsolved plans ...
                (2) history.jsonl:the history chat,and answer
        
·6.check 

    Detect execution errors and record them in state.jsonl. Use task state and LLM feedback to decide whether to retry, repair, replan, or pause.

·7.test

    it contains many  terminal command , to check the coding wether has problems, and feeding it back to the LLM , to retry

===================================================================================================================================================

·8.prompt

    different stage may have different prompt,it contains:
                (1) plan prompt : let the llm to decide plans
                (2) execute prompt : restrict the llm to run the tools strictly
                (3) check prompt :analyse the reason why the tools excute or get llm answer ... have problems
                (4) test prompt : combine the feedback to make llm decide what to do in the next step



