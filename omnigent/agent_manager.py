class AgentManager:
    def __init__(self):
        self.agents = []

    def add_agent(self, agent):
        self.agents.append(agent)

class Agent:
    def __init__(self, name):
        self.name = name

    def execute(self):
        # Código para ejecutar el agente
        pass